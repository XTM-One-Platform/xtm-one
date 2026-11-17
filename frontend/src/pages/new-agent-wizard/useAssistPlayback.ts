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
 * Plays the assistant's operations onto the wizard, one at a time, where the
 * person can see them: the step opens, the field it changes is pointed at,
 * text is typed, items are ticked one by one (AGENT_WIZARD_ASSIST).
 *
 * Each operation is applied to the draft AS IT IS when it plays - read through
 * `getDraft` - so what the person types meanwhile is kept, not overwritten by
 * a copy taken when the answer landed. With reduced motion asked for, every
 * operation applies at once, without the typing or the pauses.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";
import type { WizardDraft } from "./draft-storage";
import {
  type AssistOp, type AssistTarget, type WizardStepKey, applyAssistOp, typingFrames,
} from "./assist-plan";

/** Pauses, in ms: long enough to follow, short enough not to wait on. */
const PAUSE = { step: 600, frame: 28, afterText: 250, toggle: 260, set: 320 };

interface AssistFocus {
  step: WizardStepKey;
  target: AssistTarget | null;
}

export function useAssistPlayback({
  getDraft,
  setDraft,
  openStep,
}: {
  getDraft: () => WizardDraft;
  /** Applies the draft's fields (not its step) to the wizard. */
  setDraft: (draft: WizardDraft) => void;
  openStep: (step: WizardStepKey) => void;
}) {
  const reducedMotion = usePrefersReducedMotion();
  const [focus, setFocus] = useState<AssistFocus | null>(null);
  const [playing, setPlaying] = useState(false);
  // Bumped by every play and stop: a run whose number is no longer current
  // stops at its next await.
  const run = useRef(0);
  useEffect(() => () => {
    run.current++;
  }, []);

  const stop = useCallback(() => {
    run.current++;
    setFocus(null);
    setPlaying(false);
  }, []);

  const play = useCallback(
    async (ops: AssistOp[]) => {
      const mine = ++run.current;
      const alive = () => run.current === mine;
      const wait = (ms: number) =>
        reducedMotion ? Promise.resolve() : new Promise<void>((resolve) => setTimeout(resolve, ms));
      setPlaying(true);
      try {
        for (const op of ops) {
          if (!alive()) return;
          if (op.kind === "step") {
            openStep(op.step);
            setFocus({ step: op.step, target: null });
            await wait(PAUSE.step);
            continue;
          }
          setFocus({ step: op.step, target: op.target });
          if (op.kind === "text" && !reducedMotion) {
            for (const frame of typingFrames(getDraft()[op.field], op.value)) {
              if (!alive()) return;
              setDraft({ ...getDraft(), [op.field]: frame });
              await wait(PAUSE.frame);
            }
            await wait(PAUSE.afterText);
            continue;
          }
          setDraft(applyAssistOp(getDraft(), op));
          await wait(op.kind === "toggle" ? PAUSE.toggle : PAUSE.set);
        }
      } finally {
        if (alive()) {
          setFocus(null);
          setPlaying(false);
        }
      }
    },
    [getDraft, setDraft, openStep, reducedMotion],
  );

  return { play, stop, playing, focus };
}
