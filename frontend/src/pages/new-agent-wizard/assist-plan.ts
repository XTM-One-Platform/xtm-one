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
 * The agent wizard's assistant, as data (AGENT_WIZARD_ASSIST).
 *
 * The server answers a PATCH: only the fields the assistant changes, each list
 * whole. This module turns it into the operations the wizard plays one after
 * the other - open a step, type a text, tick or untick one item, set a value -
 * in the order the wizard walks its steps, so the person watches the draft
 * being filled where they would have filled it themselves. Pure: the player
 * (`useAssistPlayback`) owns the timing, the wizard owns the state.
 *
 * An experiment ahead of the drive-UI epic, which will give every
 * page one way to be driven; nothing outside the wizard depends on it.
 */
import type { AgentWizardPatch } from "@/types";
import type { WizardDraft } from "./draft-storage";

export type WizardStepKey =
  | "identity" | "persona" | "tools" | "integrations" | "knowledge" | "review"
  | "model" | "memory" | "guardrails" | "skills" | "handovers" | "budget";

/** What an operation points at, so the wizard can highlight it. */
export type AssistTarget =
  | "name" | "icon" | "description" | "persona" | "tools" | "integrations" | "knowledge"
  | "model" | "memory" | "guardrails" | "skills" | "handovers" | "budget";

type TextField = "name" | "description" | "persona" | "guardrails";
type ListField = "toolIds" | "integrationIds" | "kbIds" | "skillIds" | "handoverIds";
type MemoryKey = keyof WizardDraft["memory"];
type BudgetKey = keyof WizardDraft["budget"];

export type AssistOp =
  | { kind: "step"; step: WizardStepKey }
  | { kind: "text"; step: WizardStepKey; target: AssistTarget; field: TextField; value: string }
  | { kind: "toggle"; step: WizardStepKey; target: AssistTarget; field: ListField; id: string; on: boolean }
  | { kind: "icon"; step: WizardStepKey; target: AssistTarget; value: string }
  | { kind: "model"; step: WizardStepKey; target: AssistTarget; value: string }
  | { kind: "memory"; step: WizardStepKey; target: AssistTarget; key: MemoryKey; value: boolean }
  | { kind: "budget"; step: WizardStepKey; target: AssistTarget; key: BudgetKey; value: string };

/** The draft a template starts from - or a blank one - exactly as picking it
 *  in the gallery leaves it: the template's default skills start selected (the
 *  Skills step can drop them). */
export function freshDraft(
  template: {
    slug: string;
    name: string;
    icon: string;
    description: string | null;
    default_persona: string | null;
    default_skill_ids?: string[];
  } | null,
  defaultIcon: string,
): WizardDraft {
  return {
    templateSlug: template?.slug ?? null,
    stepIndex: 0,
    reviewReached: false,
    name: template?.name || "",
    icon: template?.icon || defaultIcon,
    description: template?.description || "",
    persona: template?.default_persona || "",
    integrationIds: [],
    toolIds: [],
    kbIds: [],
    modelChoice: "",
    memory: { short: true, medium: true, persistent: true, user: true },
    guardrails: "",
    skillIds: [...(template?.default_skill_ids ?? [])],
    handoverIds: [],
    budget: { cost: "", tokens: "", calls: "", seconds: "" },
  };
}

/** The draft as the assist route reads it. */
export function toAssistDraft(draft: WizardDraft, step: WizardStepKey) {
  return {
    template_slug: draft.templateSlug,
    step,
    name: draft.name,
    icon: draft.icon,
    description: draft.description,
    persona: draft.persona,
    guardrails: draft.guardrails,
    tool_ids: draft.toolIds,
    integration_ids: draft.integrationIds,
    knowledge_base_ids: draft.kbIds,
    skill_ids: draft.skillIds,
    handover_agent_ids: draft.handoverIds,
    model: draft.modelChoice,
    memory: draft.memory,
    budget: draft.budget,
  };
}

const LISTS: { patch: keyof AgentWizardPatch; field: ListField; step: WizardStepKey; target: AssistTarget }[] = [
  { patch: "tool_ids", field: "toolIds", step: "tools", target: "tools" },
  { patch: "integration_ids", field: "integrationIds", step: "integrations", target: "integrations" },
  { patch: "knowledge_base_ids", field: "kbIds", step: "knowledge", target: "knowledge" },
];
const ADVANCED_LISTS: typeof LISTS = [
  { patch: "skill_ids", field: "skillIds", step: "skills", target: "skills" },
  { patch: "handover_agent_ids", field: "handoverIds", step: "handovers", target: "handovers" },
];
const BUDGET_KEYS: [keyof NonNullable<AgentWizardPatch["budget"]>, BudgetKey][] = [
  ["max_cost_usd", "cost"],
  ["max_total_tokens", "tokens"],
  ["max_tool_calls", "calls"],
  ["max_duration_seconds", "seconds"],
];

/**
 * The operations that take `current` to `current` + `patch`, in the wizard's
 * own step order, each step opened before what it holds is changed. Fields the
 * patch leaves out, and values it repeats, produce nothing. The last operation
 * opens `focus_step`, or stays where the last change was.
 */
export function planAssist(current: WizardDraft, patch: AgentWizardPatch): AssistOp[] {
  const ops: AssistOp[] = [];
  let at: WizardStepKey | null = null;
  const open = (step: WizardStepKey) => {
    if (at !== step) ops.push({ kind: "step", step });
    at = step;
  };
  const text = (field: TextField, step: WizardStepKey, value: string | null | undefined) => {
    if (value == null || value === current[field]) return;
    open(step);
    ops.push({ kind: "text", step, target: field, field, value });
  };
  const list = ({ patch: key, field, step, target }: (typeof LISTS)[number]) => {
    const next = patch[key] as string[] | null | undefined;
    if (!next) return;
    const before = current[field];
    const removed = before.filter((id) => !next.includes(id));
    const added = next.filter((id, i) => !before.includes(id) && next.indexOf(id) === i);
    if (removed.length + added.length === 0) return;
    open(step);
    for (const id of removed) ops.push({ kind: "toggle", step, target, field, id, on: false });
    for (const id of added) ops.push({ kind: "toggle", step, target, field, id, on: true });
  };

  text("name", "identity", patch.name);
  if (patch.icon && patch.icon !== current.icon) {
    open("identity");
    ops.push({ kind: "icon", step: "identity", target: "icon", value: patch.icon });
  }
  text("description", "identity", patch.description);
  text("persona", "persona", patch.persona);
  LISTS.forEach(list);

  if (patch.model != null && patch.model !== current.modelChoice) {
    open("model");
    ops.push({ kind: "model", step: "model", target: "model", value: patch.model });
  }
  if (patch.memory) {
    for (const key of ["short", "medium", "persistent", "user"] as const) {
      const value = patch.memory[key];
      if (value == null || value === current.memory[key]) continue;
      open("memory");
      ops.push({ kind: "memory", step: "memory", target: "memory", key, value });
    }
  }
  text("guardrails", "guardrails", patch.guardrails);
  ADVANCED_LISTS.forEach(list);
  if (patch.budget) {
    for (const [patchKey, key] of BUDGET_KEYS) {
      if (!(patchKey in patch.budget)) continue;
      const raw = patch.budget[patchKey];
      const value = raw == null ? "" : String(raw);
      if (value === current.budget[key]) continue;
      open("budget");
      ops.push({ kind: "budget", step: "budget", target: "budget", key, value });
    }
  }

  if (patch.focus_step) open(patch.focus_step);
  return ops;
}

/** `draft` with one operation applied. A step operation changes nothing here:
 *  opening a step is the wizard's navigation, not the draft. */
export function applyAssistOp(draft: WizardDraft, op: AssistOp): WizardDraft {
  switch (op.kind) {
    case "step":
      return draft;
    case "text":
      return { ...draft, [op.field]: op.value };
    case "toggle": {
      const list = draft[op.field];
      if (op.on === list.includes(op.id)) return draft;
      return { ...draft, [op.field]: op.on ? [...list, op.id] : list.filter((v) => v !== op.id) };
    }
    case "icon":
      return { ...draft, icon: op.value };
    case "model":
      return { ...draft, modelChoice: op.value };
    case "memory":
      return { ...draft, memory: { ...draft.memory, [op.key]: op.value } };
    case "budget":
      return { ...draft, budget: { ...draft.budget, [op.key]: op.value } };
  }
}

/**
 * The successive values a text field shows while `to` is typed over `from`:
 * what they share is kept, the rest appears in at most `maxFrames` frames
 * whatever its length - a persona types as fast as a name. Empty when there
 * is nothing to type.
 */
export function typingFrames(from: string, to: string, maxFrames = 40): string[] {
  if (from === to) return [];
  let common = 0;
  while (common < from.length && common < to.length && from[common] === to[common]) common++;
  const remaining = to.length - common;
  if (remaining <= 0) return [to];
  const size = Math.max(1, Math.ceil(remaining / Math.max(1, maxFrames)));
  const frames: string[] = [];
  for (let end = common + size; end < to.length; end += size) frames.push(to.slice(0, end));
  frames.push(to);
  return frames;
}
