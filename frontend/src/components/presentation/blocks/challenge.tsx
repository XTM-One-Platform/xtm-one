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
 * The `challenge_answer` block: a problem and what resolves it as two
 * stacked cards joined by an arrow - the challenge in the error tone on top,
 * the answer in the success tone below. The cards are the rows of ONE grid
 * (`.deck-challenge`), so they share one width and one left edge whatever
 * their text; the connector between them is a row of its own, centred, and
 * decoration (`aria-hidden`): one stroke from the challenge card to the answer
 * card, ending in a chevron. `fill` stretches the two cards to share the
 * height the run leaves (`[data-fill]` in `presentation.css`).
 *
 * Styling is plain CSS keyed by `data-role` / `data-tone`; the tones are the
 * block's own and fixed - the two meanings every deck maps them to.
 */
import type { DeckChallengeAnswerBlock, DeckIconName, DeckMarkup } from "@/types";

import { DECK_ICON_STROKE_WIDTH, DeckIcon } from "../DeckIcon";
import { useBlockEditAttrs } from "../edit-context";
import { InlineMarkup } from "../inline-markup";

/** The eyebrow a card draws when the spec names none (the model's defaults, `presentation_spec.py`). */
const CHALLENGE_DEFAULT_LABELS = { challenge: "Challenge", answer: "Answer" } as const;
/** The answer card's own icon: a check, the success it is toned in. */
const ANSWER_ICON: DeckIconName = "circle-check";

/**
 * The connector, drawn on the 24-unit grid of Lucide's `arrow-down` at 32 px
 * wide, so its stroke and its head are that icon's. It is exactly as tall as
 * the gap between the two cards (canvas px): its row sets that gap. Touching
 * both cards, it is captured alone for the PPTX (`data-export-alone`).
 */
const ARROW_GRID = 24;
const ARROW_WIDTH_PX = 32;
const ARROW_GAP_PX = 66;
const ARROW_HEIGHT = (ARROW_GAP_PX * ARROW_GRID) / ARROW_WIDTH_PX;
const ARROW_X = ARROW_GRID / 2;
/** Half a stroke above the bottom edge: the round join of the tip then just touches the answer card. */
const ARROW_TIP_Y = ARROW_HEIGHT - DECK_ICON_STROKE_WIDTH / 2;
/** The head's arms, `m19 12-7 7-7-7` in the icon. */
const ARROW_HEAD = 7;
const ARROW_HEAD_POINTS = [
  [ARROW_X - ARROW_HEAD, ARROW_TIP_Y - ARROW_HEAD],
  [ARROW_X, ARROW_TIP_Y],
  [ARROW_X + ARROW_HEAD, ARROW_TIP_Y - ARROW_HEAD],
].join(" ");

function ChallengeArrow() {
  return (
    <svg
      className="deck-challenge-arrow"
      aria-hidden="true"
      data-export-alone="true"
      width={ARROW_WIDTH_PX}
      height={ARROW_GAP_PX}
      viewBox={`0 0 ${ARROW_GRID} ${ARROW_HEIGHT}`}
    >
      <g fill="none" stroke="currentColor" strokeWidth={DECK_ICON_STROKE_WIDTH} strokeLinecap="round" strokeLinejoin="round">
        {/* A butt end, flush with the challenge card's bottom edge. */}
        <line x1={ARROW_X} y1={0} x2={ARROW_X} y2={ARROW_TIP_Y} strokeLinecap="butt" />
        <polyline points={ARROW_HEAD_POINTS} />
      </g>
    </svg>
  );
}

function ChallengeCard({
  role,
  tone,
  icon,
  label,
  labelField,
  text,
  textField,
}: {
  role: "challenge" | "answer";
  tone: "error" | "success";
  icon: DeckIconName | undefined;
  label: DeckMarkup;
  labelField: string;
  text: DeckMarkup;
  textField: string;
}) {
  return (
    <div className="deck-challenge-card" data-role={role} data-tone={tone}>
      <div className="deck-challenge-label">
        {icon ? <DeckIcon name={icon} /> : null}
        <InlineMarkup as="span" value={label} field={labelField} />
      </div>
      <InlineMarkup as="p" className="deck-challenge-text" value={text} field={textField} />
    </div>
  );
}

export function ChallengeAnswerBlockView({ block }: { block: DeckChallengeAnswerBlock }) {
  return (
    <div className="deck-challenge" data-fill={block.fill ? "true" : undefined} {...useBlockEditAttrs()}>
      <ChallengeCard
        role="challenge"
        tone="error"
        icon={block.icon}
        label={block.challenge_label ?? CHALLENGE_DEFAULT_LABELS.challenge}
        labelField="challenge_label"
        text={block.challenge}
        textField="challenge"
      />
      <ChallengeArrow />
      <ChallengeCard
        role="answer"
        tone="success"
        icon={ANSWER_ICON}
        label={block.answer_label ?? CHALLENGE_DEFAULT_LABELS.answer}
        labelField="answer_label"
        text={block.answer}
        textField="answer"
      />
    </div>
  );
}
