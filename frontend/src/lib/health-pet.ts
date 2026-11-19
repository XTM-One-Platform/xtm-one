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
 * The Health companion's pet (#8261): the Codex pet atlas, its states and
 * which one shows. The format is the server's (`services/health/pets.py`,
 * `STATES`): 8 columns of 192x208 cells, one state a row.
 */

export const PET_COLUMNS = 8;
export const PET_CELL_WIDTH = 192;
export const PET_CELL_HEIGHT = 208;
export const PET_ROWS: Record<1 | 2, number> = { 1: 9, 2: 11 };

/** Row and frame durations (ms) of each state of the format. */
export const PET_STATES = {
  idle: { row: 0, durations: [280, 110, 110, 140, 140, 320] },
  "running-right": { row: 1, durations: [120, 120, 120, 120, 120, 120, 120, 220] },
  "running-left": { row: 2, durations: [120, 120, 120, 120, 120, 120, 120, 220] },
  waving: { row: 3, durations: [140, 140, 140, 280] },
  jumping: { row: 4, durations: [140, 140, 140, 140, 280] },
  failed: { row: 5, durations: [140, 140, 140, 140, 140, 140, 140, 240] },
  waiting: { row: 6, durations: [150, 150, 150, 150, 150, 260] },
  running: { row: 7, durations: [120, 120, 120, 120, 120, 220] },
  review: { row: 8, durations: [150, 150, 150, 150, 150, 280] },
} as const;

export type PetState = keyof typeof PET_STATES;

/** What the companion shows: the server's activity (`GET /health/me/activity`). */
export type CompanionState = "idle" | "running" | "waiting" | "failed" | "review";

/** What the chat page on screen is doing, which never reaches the server as an activity. */
export type ChatActivity = "idle" | "running";

const PRIORITY: CompanionState[] = ["failed", "waiting", "review", "running", "idle"];

/** The state shown: the more urgent of the server's and the chat's, failures first. */
export function companionState(server: CompanionState | undefined, chat: ChatActivity): CompanionState {
  const candidates = new Set<CompanionState>([server ?? "idle", chat]);
  return PRIORITY.find((state) => candidates.has(state)) ?? "idle";
}

/** The pet row each companion state plays; `waving` greets once when the pet appears. */
export const PET_ROW_FOR: Record<CompanionState, PetState> = {
  idle: "idle",
  running: "running",
  waiting: "waiting",
  failed: "failed",
  review: "review",
};

/** The frame of *state* to show *elapsed* ms into a loop of it. */
export function frameAt(state: PetState, elapsed: number): number {
  const { durations } = PET_STATES[state];
  const total = durations.reduce((sum, ms) => sum + ms, 0);
  let t = ((elapsed % total) + total) % total;
  for (let frame = 0; frame < durations.length; frame += 1) {
    if (t < durations[frame]) return frame;
    t -= durations[frame];
  }
  return 0;
}

/** CSS `background-position` of a frame, for a cell drawn at *scale*. */
export function framePosition(state: PetState, frame: number, scale: number): string {
  const x = -frame * PET_CELL_WIDTH * scale;
  const y = -PET_STATES[state].row * PET_CELL_HEIGHT * scale;
  return `${x}px ${y}px`;
}

/**
 * What the bubble says for each state, with `{name}` the Platform Health's name.
 * `idle` has no line of its own: the findings line ({@link companionFindingsLabel}) says it. @i18n-keys
 */
export const COMPANION_STATE_LABELS = {
  running: "{name} is checking the platform.",
  waiting: "A run is waiting for your approval.",
  failed: "Something did not go through. {name} has the details.",
  review: "A proposal is ready for your review.",
} as const;

/** The findings line of the bubble, most severe first. @i18n-keys */
export const COMPANION_FINDINGS_LABELS = {
  critical: "{count, plural, one {# critical finding is open} other {# critical findings are open}}",
  warning: "{count, plural, one {# warning is open} other {# warnings are open}}",
  clear: "No open finding.",
} as const;

/** Which findings line the bubble shows for the open counts (`GET /health/me/activity`). */
export function companionFindingsLabel(counts: { critical?: number; warning?: number } | undefined): {
  key: (typeof COMPANION_FINDINGS_LABELS)[keyof typeof COMPANION_FINDINGS_LABELS];
  count: number;
  severity: "critical" | "warning" | "clear";
} {
  const critical = counts?.critical ?? 0;
  const warning = counts?.warning ?? 0;
  if (critical > 0) return { key: COMPANION_FINDINGS_LABELS.critical, count: critical, severity: "critical" };
  if (warning > 0) return { key: COMPANION_FINDINGS_LABELS.warning, count: warning, severity: "warning" };
  return { key: COMPANION_FINDINGS_LABELS.clear, count: 0, severity: "clear" };
}

/** The rule a refused upload broke, by the code of its 422 (`pets.ERROR_MESSAGES`). @i18n-keys */
const UPLOAD_ERRORS = {
  too_large: "The package is larger than 8 MB.",
  not_a_package: "Upload one .zip holding pet.json and its spritesheet, or pet.json and the spritesheet.",
  manifest: "pet.json is missing or does not follow the pet format.",
  spritesheet_missing: "The spritesheet pet.json names is not in the package.",
  image_type: "The spritesheet must be a single-frame WebP or PNG image.",
  image_size: "The spritesheet must be 1536x1872 (version 1) or 1536x2288 (version 2).",
  image_unreadable: "The spritesheet could not be read.",
  slug_taken: "A pet with this id is already in the library.",
} as const;

/** The i18n key of an upload refusal by its code; `null` for a failure the server did not explain. */
export function petUploadErrorKey(error: unknown): string | null {
  const detail = (error as { detail?: unknown } | null)?.detail;
  const code = detail && typeof detail === "object" ? (detail as { code?: unknown }).code : undefined;
  return typeof code === "string" && Object.prototype.hasOwnProperty.call(UPLOAD_ERRORS, code)
    ? UPLOAD_ERRORS[code as keyof typeof UPLOAD_ERRORS]
    : null;
}
