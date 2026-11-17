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

import { Fragment, type ReactNode } from "react";
import { WaitItemList } from "@/components/chat/WaitItemList";
import { InterruptedTurnNotice } from "@/pages/chat/InterruptedTurnNotice";
import { ResumedTurnNotice } from "@/pages/chat/ResumedTurnNotice";
import { WakeTurnNotice } from "@/pages/chat/WakeTurnNotice";
import type { InterruptedTurn, ResumedFromNote, WaitItem, WakeNote } from "@/types";

export interface TranscriptFooterParts {
  /** The host's own blocks, above the wait; a falsy one is left out. */
  blocks?: ReactNode[];
  working: boolean;
  /** The turn in flight is one the platform resumed by itself, shown while it runs. */
  resumingTurn?: ResumedFromNote | null;
  /** The turn in flight was woken by finished background work, shown while it runs. */
  wakingTurn?: WakeNote | null;
  /** What a pause waits on, shown while the turn runs. */
  waitItems: WaitItem[];
  /** The conversation's last turn that stopped before its answer, shown once no turn runs. */
  interruptedTurn: InterruptedTurn | null | undefined;
  onResume?: () => void;
  onDismiss?: () => void;
  resumeDisabled?: boolean;
}

/** The panel's `transcriptFooter` on every surface that shows a conversation; `null` when it has nothing to show. */
export function transcriptFooter({
  blocks = [],
  working,
  resumingTurn,
  wakingTurn,
  waitItems,
  interruptedTurn,
  onResume,
  onDismiss,
  resumeDisabled,
}: TranscriptFooterParts): ReactNode {
  const shown = blocks.filter(Boolean);
  const waits = working ? waitItems : [];
  const interrupted = working ? null : interruptedTurn;
  const resuming = working ? resumingTurn : null;
  const waking = working ? wakingTurn : null;
  if (shown.length === 0 && waits.length === 0 && !interrupted && !resuming && !waking) return null;
  return (
    <div className="space-y-3">
      {waking && <WakeTurnNotice note={waking} variant="live" />}
      {resuming && <ResumedTurnNotice note={resuming} variant="live" />}
      {shown.map((block, i) => (
        <Fragment key={i}>{block}</Fragment>
      ))}
      {waits.length > 0 && <WaitItemList items={waits} className="px-1" />}
      {interrupted && (
        <InterruptedTurnNotice
          turn={interrupted}
          onResume={onResume}
          onDismiss={onDismiss}
          resumeDisabled={resumeDisabled}
        />
      )}
    </div>
  );
}
