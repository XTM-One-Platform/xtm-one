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
 * Presenter controls of the live polls on one slide (xtm-one #10157).
 *
 * Drawn OUTSIDE the slide, in app chrome at its real size: the slide is what
 * the audience sees (the projected screen, a public link), so the buttons never
 * appear on it, and a control scaled with the slide would shrink under 36 px
 * on a small viewer. The editor's stage shows them under the deck (not while
 * presenting fullscreen: that screen is the room's); the speaker view shows
 * them under the current slide. Only a reader the polls endpoint answers
 * `can_control` for (the owner, an editor) gets them.
 *
 * It reads the same query as the deck (`useDeckPolls`, one cache entry), so a
 * control and the slide never disagree.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useControlDeckPoll, useDeckPolls } from "@/hooks/api/usePresentationApi";
import { useFormatter } from "@/i18n";
import type { DeckPollBlock, PollState } from "@/types";

import { deckMayHandleKey } from "./deck-keys";
import { pollBlocksOf } from "./poll-context";

/**
 * Whether a conditional reveal (`after_votes`, `after_seconds`, #10411) already
 * marks the answer of the open question (every quiz question of a layout "all"
 * poll): Reveal and Hide would change nothing then, so neither is offered
 * (UX round 1 B1 of #10417). Read from each question's `condition_met`, the
 * condition alone, whatever the presenter's own reveal.
 */
function shownByCondition(block: DeckPollBlock, state: PollState): boolean {
  if (block.reveal !== "after_votes" && block.reveal !== "after_seconds") return false;
  const every = block.layout === "all" || state.layout === "all";
  const quizzes = state.questions.filter((q) => q.kind === "quiz" && (every || q.id === state.active_question));
  return quizzes.length > 0 && quizzes.every((q) => q.condition_met === true);
}

function OnePoll({
  block,
  state,
  control,
}: {
  block: DeckPollBlock;
  state: PollState;
  control: (action: string, questionId?: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const [confirmReset, setConfirmReset] = useState(false);
  // A layout "all" poll opens every question at once (xtm-one #10235): no counter, no stepping.
  const every = block.layout === "all" || state.layout === "all";
  const index = block.questions.findIndex((q) => q.id === state.active_question);
  const question = block.questions[index];
  // `reveal: live` (#10315) is always marked and `never` (#10411) never: nothing to reveal by hand.
  // The conditional modes (#10411) keep the button, to show the answer before the condition.
  const hasQuiz =
    block.reveal !== "live" &&
    block.reveal !== "never" &&
    !shownByCondition(block, state) &&
    (every ? block.questions.some((q) => q.kind === "quiz") : question?.kind === "quiz");
  // Centred under the slide like the fullscreen bar, every control an outlined button (#10315 change 6).
  return (
    <div
      role="toolbar"
      aria-label={t_i18n("Poll controls")}
      className="flex flex-wrap items-center justify-center gap-2"
      data-align="center"
      data-deck-ignore-keys="true"
    >
      {every ? null : (
        <>
          <span className="me-1 text-sm text-default-secondary">
            {t_i18n("Question {current} of {count}", { current: index + 1, count: block.questions.length })}
          </span>
          <Button variant="outline" disabled={index <= 0} onClick={() => control("open", block.questions[index - 1]?.id)}>
            {t_i18n("Previous question")}
          </Button>
          <Button variant="outline" disabled={index >= block.questions.length - 1} onClick={() => control("next")}>
            {t_i18n("Next question")}
          </Button>
        </>
      )}
      {hasQuiz ? (
        <Button variant="outline" onClick={() => control(state.revealed ? "hide" : "reveal")}>
          {state.revealed ? t_i18n("Hide the answer") : t_i18n("Reveal the answer")}
        </Button>
      ) : null}
      <Button variant="outline" onClick={() => control(state.locked ? "unlock" : "lock")}>
        {state.locked ? t_i18n("Reopen voting") : t_i18n("Lock voting")}
      </Button>
      {confirmReset ? (
        <>
          <Button variant="outline" onClick={() => setConfirmReset(false)}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            variant="destructive"
            onClick={() => {
              setConfirmReset(false);
              control("reset");
            }}
          >
            {t_i18n("Delete all votes")}
          </Button>
        </>
      ) : (
        <Button variant="outline" onClick={() => setConfirmReset(true)}>
          {t_i18n("Reset votes")}
        </Button>
      )}
    </div>
  );
}

/** The confirmation each action shows on the presented screen (xtm-one #10235); static keys. */
function actionToast(action: string, t_i18n: (key: string) => string): string | null {
  switch (action) {
    case "lock":
      return t_i18n("Voting locked");
    case "unlock":
      return t_i18n("Voting reopened");
    case "reveal":
      return t_i18n("Answer revealed");
    case "hide":
      return t_i18n("Answer hidden");
    case "reset":
      return t_i18n("Votes reset");
    case "next":
      return t_i18n("Next question");
    case "open":
      return t_i18n("Previous question");
    default:
      return null;
  }
}

/** How long the presented screen's confirmation stays up. */
const POLL_TOAST_MS = 2000;

/**
 * The poll controls of a deck presented FULLSCREEN on a single screen (xtm-one
 * #10235): the presenter has no speaker view and no bar under the slide.
 *
 *   - A bar along the bottom edge, inside the stage (the fullscreen element),
 *     that shows on pointer activity and hides with the deck toolbar once the
 *     stage goes idle (`data-deck-idle`), so it is never on the room's screen
 *     while nobody touches the laptop.
 *   - Keys, without leaving fullscreen: `L` lock / reopen, `V` reveal / hide
 *     the quiz answer, `Shift+R` reset (Enter confirms, Esc cancels), and on a
 *     guided poll `N` / `P` step questions. The arrows stay the deck's.
 *     Caught in the capture phase so reveal never also acts on them (its `V`
 *     blacks the screen, its `N` / `P` turn the page).
 *   - A short confirmation per action, drawn inside the stage: the app's toast
 *     region is outside the fullscreen element and would not paint.
 *
 * An autonomous poll (`layout: all`) needs none of it: every question is open
 * and the results are live; the controls are there, never required. A poll
 * with `controls: none` (#10315) gets nothing here: no bar, no toast, no key;
 * it is driven from the controls under the deck, the speaker view or the tool.
 */
export function PollPresentBar({
  presentationId,
  slide,
  stage,
}: {
  presentationId: string;
  slide: unknown;
  stage: HTMLElement | null;
}) {
  const { t_i18n } = useFormatter();
  const blocks = pollBlocksOf(slide);
  const polls = useDeckPolls(presentationId, blocks.length > 0);
  const mutation = useControlDeckPoll(presentationId);
  const [confirmReset, setConfirmReset] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef(0);
  const block = blocks[0];
  const state = block ? polls.data?.polls.find((poll) => poll.poll_id === block.id) : undefined;
  const handsFree = block?.controls === "none";
  const canControl = !!block && !!state && !!polls.data?.can_control && !handsFree;
  const guided = !!block && block.layout !== "all" && state?.layout !== "all";

  const control = useCallback(
    (action: string, questionId?: string) => {
      if (!block) return;
      mutation.mutate({ pollId: block.id, action, questionId });
      window.clearTimeout(toastTimer.current);
      setToast(actionToast(action, t_i18n));
      toastTimer.current = window.setTimeout(() => setToast(null), POLL_TOAST_MS);
    },
    [block, mutation, t_i18n],
  );
  useEffect(() => () => window.clearTimeout(toastTimer.current), []);
  // A new slide drops a pending reset.
  useEffect(() => setConfirmReset(false), [block?.id]);

  const index = block && state ? block.questions.findIndex((q) => q.id === state.active_question) : -1;
  const latest = useRef({ canControl, guided, state, block, index, confirmReset, control });
  latest.current = { canControl, guided, state, block, index, confirmReset, control };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const now = latest.current;
      if (!now.canControl || !now.state || !now.block) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (now.confirmReset) {
        if (event.key === "Enter" || event.key === "Escape") {
          event.preventDefault();
          event.stopImmediatePropagation();
          setConfirmReset(false);
          if (event.key === "Enter") now.control("reset");
        }
        return;
      }
      if (!deckMayHandleKey(event, stage)) return;
      const key = event.key.toLowerCase();
      let handled = true;
      if (key === "l" && !event.shiftKey) now.control(now.state.locked ? "unlock" : "lock");
      else if (key === "v" && !event.shiftKey && !shownByCondition(now.block, now.state)) {
        now.control(now.state.revealed ? "hide" : "reveal");
      } else if (key === "r" && event.shiftKey) setConfirmReset(true);
      else if (key === "n" && !event.shiftKey && now.guided) now.control("next");
      else if (key === "p" && !event.shiftKey && now.guided) {
        const previous = now.block.questions[now.index - 1];
        if (previous) now.control("open", previous.id);
      } else handled = false;
      if (handled) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [stage]);

  if (!canControl || !block || !state) return null;
  return (
    <>
      <div
        className="deck-poll-present-bar"
        data-confirm={confirmReset ? "true" : undefined}
        data-testid="presentation.poll-present-bar"
      >
        {confirmReset ? (
          <div className="flex items-center gap-2" role="alertdialog" aria-label={t_i18n("Reset votes")}>
            <span className="text-sm">{t_i18n("Delete all votes? Enter to confirm, Esc to cancel")}</span>
            <Button variant="outline" onClick={() => setConfirmReset(false)}>
              {t_i18n("Cancel")}
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                setConfirmReset(false);
                control("reset");
              }}
            >
              {t_i18n("Delete all votes")}
            </Button>
          </div>
        ) : (
          <OnePoll block={block} state={state} control={control} />
        )}
      </div>
      {toast ? (
        <div className="deck-poll-present-toast" role="status" data-testid="presentation.poll-toast">
          {toast}
        </div>
      ) : null}
    </>
  );
}

/** The controls of every poll on `slide`; nothing when it holds none or the reader cannot control. */
export function PollControls({ presentationId, slide }: { presentationId: string; slide: unknown }) {
  const blocks = pollBlocksOf(slide);
  const polls = useDeckPolls(presentationId, blocks.length > 0);
  const mutation = useControlDeckPoll(presentationId);
  if (!blocks.length || !polls.data?.can_control) return null;
  return (
    <div className="flex w-full flex-col items-stretch gap-2 px-1 py-2">
      {blocks.map((block) => {
        const state = polls.data?.polls.find((poll) => poll.poll_id === block.id);
        if (!state) return null;
        return (
          <OnePoll
            key={block.id}
            block={block}
            state={state}
            control={(action, questionId) => mutation.mutate({ pollId: block.id, action, questionId })}
          />
        );
      })}
    </div>
  );
}
