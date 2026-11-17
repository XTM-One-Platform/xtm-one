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
 * A chat turn the platform resumed by itself after a cut.
 *
 * The backend opens such a turn with a `turn_resuming` frame (handled by
 * that name in `useTurnFrames`, mirroring `cut_turn_resume.RESUMING_FRAME_TYPE`)
 * (`cut_turn_resume.resuming_frame`: cause, resume, of) and stores the same
 * fields on the reply it ends with (`metadata.resumed_from`, served as the
 * message's `resumed_from`). Both read through here, so the live line and
 * the persisted marker say the same thing.
 */
import type { ResumedFromNote } from "@/types";

function positiveInt(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null;
}

/** The note a `turn_resuming` frame (or a message's `resumed_from`) carries. */
export function readResumingFrame(data: unknown): ResumedFromNote | null {
  if (!data || typeof data !== "object") return null;
  const raw = data as Record<string, unknown>;
  return {
    cause: typeof raw.cause === "string" && raw.cause ? raw.cause : null,
    resume: positiveInt(raw.resume),
    of: positiveInt(raw.of),
    cut_at: typeof raw.cut_at === "string" && raw.cut_at ? raw.cut_at : null,
  };
}
