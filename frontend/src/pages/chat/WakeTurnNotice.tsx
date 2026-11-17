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

import { Icon } from "@filigran/design-system";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { WakeNote } from "@/types";

interface WakeTurnNoticeProps {
  note: WakeNote;
  /** `live`: while the wake turn runs; `marker`: above its stored reply. */
  variant: "live" | "marker";
}

/**
 * Says that finished background work woke the conversation: the reply below
 * reports on it and answers no new message (#11796). Live while the wake turn
 * runs, then a compact marker above the reply it ended with, so a reload,
 * another device and a shared transcript never read it as a second answer to
 * the last message. The same family as `ResumedTurnNotice`.
 *
 * An A2A partner's answer (`kind` partner) is named as the partner's answer:
 * the user asked a partner, he never started a background task. A sub-agent
 * task, a job and a note stored before the kind existed keep the task wording.
 */
export function WakeTurnNotice({ note, variant }: WakeTurnNoticeProps) {
  const { t_i18n } = useFormatter();
  const partner = note.kind === "partner";
  let text: string;
  if (partner) {
    text = note.agent_name ? t_i18n("{agent} answered", { agent: note.agent_name }) : t_i18n("The partner answered");
  } else {
    text = note.agent_name
      ? t_i18n("Background task of {agent} finished", { agent: note.agent_name })
      : t_i18n("Background task finished");
  }
  return (
    <div
      role="status"
      data-testid={variant === "live" ? "wake-turn-live" : "wake-turn-marker"}
      className={cn(
        "flex max-w-2xl items-center gap-2 text-default-secondary",
        variant === "live"
          ? "ml-11 rounded-sm border border-feedback-info-primary/25 bg-feedback-info-primary/[0.04] px-4 py-3"
          : "mb-1",
      )}
    >
      <Icon name={partner ? "message-square" : "circle-check"} size={16} className="shrink-0" />
      <p className="content-caption">{text}</p>
    </div>
  );
}
