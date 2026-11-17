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
 * When and how the slide editor saves on its own - the pure half of
 * `useDeckAutosave`, kept free of React so `yarn test:unit` pins it.
 *
 * A change is saved once the person pauses (`AUTOSAVE_IDLE_MS`), and never
 * later than `AUTOSAVE_MAX_WAIT_MS` after the first change still unsaved, so
 * a long burst of typing is written as it goes. A save that did not reach the
 * server is retried with a doubling delay; one the server refused for what
 * the draft holds (a 422) waits for the draft to change; one refused because
 * a collaborator saved first (a 409) waits for their revision to be merged,
 * then goes again.
 */
import type { Presentation, PresentationUpdate } from "@/types";

import { normalizeChrome, type DeckDraft } from "./presentation-editor.ts";

/** A save waits for the changes to pause this long... */
export const AUTOSAVE_IDLE_MS = 800;
/** ...but never longer than this after the first change still unsaved. */
export const AUTOSAVE_MAX_WAIT_MS = 4_000;
/** The first retry of a save that did not reach the server; doubled per failure, up to the cap. */
export const AUTOSAVE_RETRY_BASE_MS = 2_000;
export const AUTOSAVE_RETRY_MAX_MS = 30_000;
/** How long after a save the person still reads as "editing" to the others in the room. */
export const EDITING_LINGER_MS = 20_000;
/** How long leaving the editor waits for the last changes to be saved before it asks. */
export const LEAVE_FLUSH_TIMEOUT_MS = 10_000;

/** In how many milliseconds the pending changes are due to be saved. */
export function autosaveDelay(now: number, lastChangeAt: number, firstChangeAt: number): number {
  const due = Math.min(lastChangeAt + AUTOSAVE_IDLE_MS, firstChangeAt + AUTOSAVE_MAX_WAIT_MS);
  return Math.max(0, due - now);
}

/**
 * The wait before the `failures`-th retry of a save that did not reach the
 * server - never shorter than the server asked (`Retry-After` of a 429, in
 * seconds).
 */
export function retryDelay(failures: number, retryAfterSeconds?: number): number {
  const backoff = Math.min(AUTOSAVE_RETRY_MAX_MS, AUTOSAVE_RETRY_BASE_MS * 2 ** Math.max(0, failures - 1));
  return retryAfterSeconds && retryAfterSeconds > 0 ? Math.max(backoff, retryAfterSeconds * 1000) : backoff;
}

/**
 * The largest body a save sent as the page goes away may carry: the browser
 * refuses a `keepalive` request past 64 KiB (all such requests together), so
 * a bigger deck goes as a normal request - the leave prompt stands in front
 * of the tab closing while a change is unsaved.
 */
export const KEEPALIVE_BODY_MAX_BYTES = 60_000;

/** Whether `body` may go out as a `keepalive` request. */
export function keepaliveFits(body: unknown): boolean {
  return new TextEncoder().encode(JSON.stringify(body)).length <= KEEPALIVE_BODY_MAX_BYTES;
}

/**
 * What a failed save means for the next one: `conflict` (409) - a newer
 * revision to merge first; `invalid` (400 / 422) - the draft as it stands is
 * refused, the next save waits for it to change; `denied` (403 / 404) - the
 * person may no longer edit this deck; `unauthorized` (401) - the session is
 * gone, so no retry can succeed until the person signs in again: the save
 * stops and says so, the changes kept on the page; `transport` -
 * anything else (offline, a timeout, a 5xx, a 429 - after its `Retry-After`),
 * retried.
 */
export type SaveFailureKind = "conflict" | "invalid" | "denied" | "unauthorized" | "transport";

export function failureKind(status: number | undefined): SaveFailureKind {
  if (status === 409) return "conflict";
  if (status === 400 || status === 413 || status === 422) return "invalid";
  if (status === 401) return "unauthorized";
  if (status === 403 || status === 404) return "denied";
  return "transport";
}

/**
 * Whether no further save can help until the person acts (a refused right, an
 * ended session). Takes a save's outcome as well, where anything but a failure
 * kind reads as not final.
 */
export function failureIsFinal(kind: string | null | undefined): boolean {
  return kind === "denied" || kind === "unauthorized";
}

/**
 * What the status line says. `saved`: nothing left to write. `pending`: a
 * save is due. `saving`: one is on its way. `syncing`: a collaborator's
 * revision is being merged in first. `retrying`: the last save did not reach
 * the server. `offline`: the browser is offline - the changes are kept.
 * `invalid`: the server refused the draft as it stands. `denied`: the person
 * may no longer edit the deck. `unauthorized`: the session ended, the changes
 * wait for the person to sign in again.
 */
export type AutosaveState =
  | "saved"
  | "pending"
  | "saving"
  | "syncing"
  | "retrying"
  | "offline"
  | "invalid"
  | "denied"
  | "unauthorized";

export function autosaveState(input: {
  dirty: boolean;
  saving: boolean;
  outOfDate: boolean;
  online: boolean;
  failure: SaveFailureKind | null;
  /** The failure concerns the draft as it is now (a 422 of an older draft no longer holds). */
  failureOnDraft: boolean;
}): AutosaveState {
  if (input.saving) return "saving";
  if (!input.dirty && !input.outOfDate) return "saved";
  if (input.failure === "denied") return "denied";
  if (input.failure === "unauthorized") return "unauthorized";
  if (input.failure === "invalid" && input.failureOnDraft) return "invalid";
  if (!input.online) return "offline";
  if (input.outOfDate) return "syncing";
  if (input.failure === "transport") return "retrying";
  return "pending";
}

/**
 * The `PATCH` body that saves `draft` over `presentation` (the revision it
 * extends): the edited pieces and `expected_revision`, never a raw spec. The
 * template re-link travels only when the pick changed (the server reads the
 * field's PRESENCE as the intent), and the chrome only when it differs
 * (it is replaced wholesale).
 */
export function saveBody(presentation: Presentation, draft: DeckDraft, revision: number): PresentationUpdate & { id: string } {
  const templateChanged = draft.template_container_id !== presentation.template_container_id;
  const chromeChanged = JSON.stringify(draft.chrome) !== JSON.stringify(normalizeChrome(presentation.spec.chrome));
  return {
    id: presentation.id,
    title: draft.title.trim() || presentation.title,
    theme: draft.theme,
    transition: draft.transition,
    slides: draft.slides,
    ...(templateChanged ? { template_container_id: draft.template_container_id } : {}),
    ...(chromeChanged ? { chrome: draft.chrome } : {}),
    expected_revision: revision,
  };
}
