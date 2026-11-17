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

import { RotateCw } from "lucide-react";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { ResumedFromNote } from "@/types";

interface ResumedTurnNoticeProps {
  note: ResumedFromNote;
  /** `live`: while the resumed turn runs; `marker`: above its stored reply. */
  variant: "live" | "marker";
}

/**
 * Says that the platform resumed a cut turn by itself: informational,
 * the same family as `InterruptedTurnNotice` without its warning. Live while
 * the resume runs, then a compact marker above the reply it ended with, so a
 * reload, another device and a shared transcript still tell a resumed answer
 * from a fresh one. Drawn only where no `InterruptedReplyCard` already says
 * the cut (a resume whose cut stored no reply): never a second notice for one
 * interruption. No resume counter.
 */
export function ResumedTurnNotice({ note, variant }: ResumedTurnNoticeProps) {
  const { t_i18n } = useFormatter();
  const cause =
    note.cause === "recycle" ? t_i18n("a worker recycle") : t_i18n("a platform restart");
  const text = t_i18n("Resumed by itself after {cause}", { cause });
  return (
    <div
      role="status"
      data-testid={variant === "live" ? "resumed-turn-live" : "resumed-turn-marker"}
      className={cn(
        "flex max-w-2xl items-center gap-2 text-default-secondary",
        // Live, it sits in the transcript, aligned with the replies, on the
        // tinted surface and padding of `InterruptedTurnNotice` (same family,
        // info instead of alert); as a marker, inside the reply's
        // bubble, above its text.
        variant === "live"
          ? "ml-11 rounded-sm border border-feedback-info-primary/25 bg-feedback-info-primary/[0.04] px-4 py-3"
          : "mb-1",
      )}
    >
      <RotateCw className="h-3.5 w-3.5 shrink-0" aria-hidden />
      <p className="content-caption">{text}</p>
    </div>
  );
}
