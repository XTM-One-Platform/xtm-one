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
 * The draft of a Deep Investigation pack being built: pure edits the
 * builder page applies, unit-tested in `pack-draft.test.ts`.
 *
 * The server validates every save (a 422 names what is wrong); these keep the
 * draft coherent while it is edited: a step's key stays unique, removing a step
 * drops the pivots into it and the settings it exposed, moving a step never
 * leaves a pivot pointing backwards.
 */
import type {
  InvestigationPackFields,
  PackExposedSetting,
  PackSourceSpec,
  PackStepSpec,
} from "../../../types/index.ts";
import { validationMessages } from "../../../lib/error-detail.ts";

export const MAX_STEPS = 12;
const MAX_STEP_SOURCES = 12;
const MAX_EXPOSED_SETTINGS = 6;

const NEW_PACK_TYPES = ["domain", "url"];

/** A new pack: one step answering domains, with a first source - the first
 *  public lookup the server offers that answers those subjects (NVD, first in
 *  the catalog, answers CVEs only and would be refused) - so it can be saved
 *  as it is. */
export function emptyDraft(references: { name: string; subject_types: string[] }[] = []): InvestigationPackFields {
  const firstReference = references.find((r) => r.subject_types.some((t) => NEW_PACK_TYPES.includes(t)))?.name;
  return {
    name: "",
    description: "",
    subject_types: [...NEW_PACK_TYPES],
    recommended: false,
    max_iterations: 10,
    definition: {
      steps: [
        {
          key: "qualify",
          label: "Qualify the subject",
          description: "",
          icon: "target",
          sources: firstReference ? [{ kind: "reference", reference: firstReference }] : [],
        },
      ],
      exposed: [],
    },
  };
}

/** A step key from its label: lowercase letters, digits and '-', unique. */
export function stepKey(label: string, taken: string[]): string {
  const base =
    label
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 36) || "step";
  let key = base;
  for (let n = 2; taken.includes(key); n++) key = `${base}-${n}`;
  return key;
}

export function addStep(draft: InvestigationPackFields, label: string): InvestigationPackFields {
  const steps = draft.definition.steps;
  if (steps.length >= MAX_STEPS) return draft;
  const step: PackStepSpec = {
    key: stepKey(label, steps.map((s) => s.key)),
    label,
    description: "",
    icon: "target",
    sources: [],
  };
  return withSteps(draft, [...steps, step]);
}

export function updateStep(
  draft: InvestigationPackFields,
  index: number,
  patch: Partial<Omit<PackStepSpec, "key" | "sources">>,
): InvestigationPackFields {
  return withSteps(
    draft,
    draft.definition.steps.map((s, i) => (i === index ? { ...s, ...patch } : s)),
  );
}

/** Remove a step, with the pivots into it and the settings it exposed. */
export function removeStep(draft: InvestigationPackFields, index: number): InvestigationPackFields {
  const removed = draft.definition.steps[index];
  if (!removed) return draft;
  const steps = draft.definition.steps
    .filter((_s, i) => i !== index)
    .map((s) => ({
      ...s,
      sources: s.sources.map((src) => (src.pivot_step === removed.key ? { ...src, pivot_step: null } : src)),
    }));
  return {
    ...draft,
    definition: {
      steps,
      exposed: (draft.definition.exposed ?? []).filter((e) => e.step !== removed.key),
    },
  };
}

/** Move a step up (-1) or down (+1); a pivot that would point backwards is
 *  dropped, since the server refuses it. */
export function moveStep(draft: InvestigationPackFields, index: number, delta: -1 | 1): InvestigationPackFields {
  const steps = [...draft.definition.steps];
  const target = index + delta;
  if (target < 0 || target >= steps.length) return draft;
  [steps[index], steps[target]] = [steps[target], steps[index]];
  const position = new Map(steps.map((s, i) => [s.key, i]));
  return withSteps(
    draft,
    steps.map((s, i) => ({
      ...s,
      sources: s.sources.map((src) =>
        src.pivot_step && (position.get(src.pivot_step) ?? -1) < i ? { ...src, pivot_step: null } : src,
      ),
    })),
  );
}

/** A source of ``kind`` with the defaults a run needs. */
export function newSource(kind: PackSourceSpec["kind"], reference?: string): PackSourceSpec {
  if (kind === "reference") return { kind, reference: reference ?? null };
  if (kind === "knowledge_base") return { kind, knowledge_base_ids: [] };
  if (kind === "mcp_tool") return { kind, server_id: null };
  if (kind === "http") return { kind, url_template: null };
  return { kind };
}

export function addSource(draft: InvestigationPackFields, stepIndex: number, source: PackSourceSpec): InvestigationPackFields {
  return withSteps(
    draft,
    draft.definition.steps.map((s, i) =>
      i === stepIndex && s.sources.length < MAX_STEP_SOURCES ? { ...s, sources: [...s.sources, source] } : s,
    ),
  );
}

export function updateSource(
  draft: InvestigationPackFields,
  stepIndex: number,
  sourceIndex: number,
  patch: Partial<PackSourceSpec>,
): InvestigationPackFields {
  return withSteps(
    draft,
    draft.definition.steps.map((s, i) =>
      i === stepIndex
        ? { ...s, sources: s.sources.map((src, j) => (j === sourceIndex ? { ...src, ...patch } : src)) }
        : s,
    ),
  );
}

/** Remove a source; the settings exposed on it go, and those on the sources
 *  after it follow their source's new position. */
export function removeSource(draft: InvestigationPackFields, stepIndex: number, sourceIndex: number): InvestigationPackFields {
  const step = draft.definition.steps[stepIndex];
  if (!step) return draft;
  const exposed = (draft.definition.exposed ?? [])
    .filter((e) => !(e.step === step.key && e.source === sourceIndex))
    .map((e) => (e.step === step.key && e.source > sourceIndex ? { ...e, source: e.source - 1 } : e));
  return {
    ...draft,
    definition: {
      steps: draft.definition.steps.map((s, i) =>
        i === stepIndex ? { ...s, sources: s.sources.filter((_src, j) => j !== sourceIndex) } : s,
      ),
      exposed,
    },
  };
}

/** Offer (or stop offering) a source's setting at launch, with a default. */
export function setExposed(
  draft: InvestigationPackFields,
  setting: Omit<PackExposedSetting, "label">,
  on: boolean,
): InvestigationPackFields {
  const same = (e: PackExposedSetting) =>
    e.step === setting.step && e.source === setting.source && e.setting === setting.setting;
  const rest = (draft.definition.exposed ?? []).filter((e) => !same(e));
  if (on && rest.length >= MAX_EXPOSED_SETTINGS) return draft;
  return { ...draft, definition: { ...draft.definition, exposed: on ? [...rest, setting] : rest } };
}

/** The steps a source may pivot into: its own and the later ones. */
export function pivotTargets(draft: InvestigationPackFields, stepIndex: number): PackStepSpec[] {
  return draft.definition.steps.slice(stepIndex);
}

function withSteps(draft: InvestigationPackFields, steps: PackStepSpec[]): InvestigationPackFields {
  return { ...draft, definition: { ...draft.definition, steps } };
}

/** What a refused save says, readable: a 422 from the body model carries a
 *  list of `{loc, msg}` (`lib/error-detail.ts`). */
export function refusalText(error: unknown): string | null {
  if (!error) return null;
  const messages = validationMessages((error as { detail?: unknown }).detail);
  if (messages.length) return messages.join(" · ");
  return error instanceof Error ? error.message : String(error);
}

/** A turn of the builder's assistant panel. */
export interface AssistTurn {
  role: "user" | "assistant";
  content: string;
  /** A round that failed here: shown, never sent back as the model's answer. */
  failed?: boolean;
}

/** The most turns one round may carry (`PackAssistRequest.history`). */
export const MAX_ASSIST_HISTORY = 20;

/** The conversation sent with the next round: what was said, not the panel's
 *  own error bubbles, and its newest turns only: past the bound every round
 *  was a 422 until the page was reloaded. */
export function assistHistory(turns: AssistTurn[]): { role: "user" | "assistant"; content: string }[] {
  return turns
    .filter((t) => !t.failed)
    .slice(-MAX_ASSIST_HISTORY)
    .map(({ role, content }) => ({ role, content }));
}
