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
 * The `poll` block (xtm-one #10157): a live audience poll on a slide.
 *
 * `layout: "active"` (the default): left, the open question and its results
 * as votes arrive (bars with counts and shares for choice / quiz, a word
 * cloud, a rating's average and distribution); right, the short link and its
 * QR code, so a room joins from a phone without signing in (`/v/CODE`,
 * `pages/PollVote.tsx`). `layout: "all"`: the link and the voters across the
 * top, then every question side by side with its live results - every question
 * is open to the audience at once (xtm-one #10235): no question is marked as
 * current and the presenter steps nothing, so it runs in presentation mode alone.
 *
 * The live state comes from `DeckPollsContext` (read every 2 s); without it
 * (public link, PDF export, a reader without a session) the block draws its
 * questions and the link placeholder only. The presenter controls are never
 * drawn here: the slide is what the audience sees. `PollControls` draws them
 * outside the slide, under the editor's stage and in the speaker view.
 *
 * Styling is plain CSS in `presentation.css`, keyed by `data-*` attributes.
 */
import { useFormatter } from "@/i18n";
import type { DeckPollBlock, DeckPollQuestion, PollQuestionState, PollState } from "@/types";

import { useBlockEditAttrs } from "../edit-context";
import { InlineMarkup, PlainText } from "../inline-markup";
import { usePollState } from "../poll-context";

function share(count: number, total: number): number {
  return total > 0 ? Math.round((count / total) * 100) : 0;
}

function Bars({ question, live, revealed }: { question: PollQuestionState; live: boolean; revealed: boolean }) {
  const counts = question.results?.counts ?? question.options.map(() => 0);
  const total = question.results?.total ?? 0;
  return (
    <ol className="deck-poll-bars">
      {question.options.map((option, index) => {
        const count = counts[index] ?? 0;
        const right = revealed && question.correct === index;
        return (
          <li key={index} className="deck-poll-bar" data-correct={right ? "true" : undefined}>
            {/* Up to two lines (on its own line in a layout "all" cell), an ellipsis only past them, the whole label in the tooltip. */}
            <span className="deck-poll-bar-label" title={option}>
              <PlainText value={option} />
            </span>
            <span className="deck-poll-bar-track">
              <span className="deck-poll-bar-fill" style={{ width: `${live ? share(count, total) : 0}%` }} />
            </span>
            <span className="deck-poll-bar-value">{live ? `${count} · ${share(count, total)}%` : ""}</span>
          </li>
        );
      })}
    </ol>
  );
}

function WordCloud({ question }: { question: PollQuestionState }) {
  const words = question.results?.words ?? [];
  const top = words[0]?.count ?? 1;
  return (
    <div className="deck-poll-cloud">
      {words.map((word) => (
        <span key={word.text} className="deck-poll-word" style={{ fontSize: `${1 + (word.count / top) * 1.6}em` }}>
          {word.text}
        </span>
      ))}
    </div>
  );
}

function Rating({ question }: { question: PollQuestionState }) {
  const counts = question.results?.counts ?? [0, 0, 0, 0, 0];
  const total = question.results?.total ?? 0;
  return (
    <div className="deck-poll-rating">
      <div className="deck-poll-rating-average">{question.results?.average ?? "-"}</div>
      <Bars
        question={{ ...question, options: ["1", "2", "3", "4", "5"], results: { id: question.id, kind: "rating", counts, total } }}
        live
        revealed={false}
      />
    </div>
  );
}

function fromSpec(question: DeckPollQuestion): PollQuestionState {
  return {
    id: question.id,
    kind: question.kind ?? "choice",
    prompt: question.prompt,
    options: question.options ?? [],
    max_words: question.max_words ?? 1,
    correct: question.correct ?? null,
  };
}

/** The short link in two lines: the host small, `/v/CODE` large and never broken inside the code. */
function JoinLink({ state }: { state: PollState | undefined }) {
  const bare = state ? state.url.replace(/^https?:\/\//, "") : "";
  const cut = bare.lastIndexOf("/v/");
  const host = cut > 0 ? bare.slice(0, cut) : "";
  const path = cut >= 0 ? bare.slice(cut) : "/v/…";
  return (
    <div className="deck-poll-link">
      {host ? <span className="deck-poll-link-host">{host}</span> : null}
      <span className="deck-poll-link-code">{path}</span>
    </div>
  );
}

/** One question's results, or the line that says none came in yet. */
function Results({
  question,
  showResults,
  afterLock,
  revealed,
}: {
  question: PollQuestionState;
  showResults: boolean;
  /** The block holds its results until voting is locked (`show_results: after_lock`). */
  afterLock: boolean;
  revealed: boolean;
}) {
  const { t_i18n } = useFormatter();
  const total = question.results?.total ?? 0;
  const right =
    question.kind === "quiz" && question.correct != null ? share(question.results?.counts?.[question.correct] ?? 0, total) : 0;
  return (
    <>
      {question.kind === "choice" || question.kind === "quiz" ? (
        <Bars question={question} live={showResults} revealed={revealed} />
      ) : null}
      {question.kind === "word_cloud" && showResults ? <WordCloud question={question} /> : null}
      {question.kind === "rating" && showResults ? <Rating question={question} /> : null}
      {showResults && total === 0 ? <div className="deck-poll-waiting">{t_i18n("Waiting for the first vote")}</div> : null}
      {!showResults && afterLock && question.kind !== "choice" && question.kind !== "quiz" ? (
        <div className="deck-poll-waiting">{t_i18n("Results show once voting is locked")}</div>
      ) : null}
      {/* The share comes with the first vote: at 0 votes the waiting line stands alone (a vote reset included). */}
      {revealed && question.kind === "quiz" && total > 0 ? (
        <div className="deck-poll-score">{t_i18n("{share}% answered right", { share: right })}</div>
      ) : null}
    </>
  );
}

export function PollBlockView({ block }: { block: DeckPollBlock }) {
  const { t_i18n } = useFormatter();
  const editAttrs = useBlockEditAttrs();
  const state = usePollState(block.id);

  const activeId = state?.active_question ?? block.questions[0]?.id;
  const afterLock = block.show_results === "after_lock";
  const showResults = !!state && (!afterLock || state.locked);
  // `reveal: live` (#10315): the quiz answer is marked from the start, no reveal step.
  const revealed = block.reveal !== "never" && (!!state?.revealed || block.reveal === "live");
  // Each quiz question carries its own `revealed` (#10411: after N votes, S seconds after the first
  // vote, never); a state from an older server has none and follows the poll's.
  const revealedOf = (question: PollQuestionState) => question.revealed ?? revealed;
  const showQr = block.show_qr !== false;
  const questionOf = (spec: DeckPollQuestion) => state?.questions.find((q) => q.id === spec.id) ?? fromSpec(spec);
  // No live state (a public link, an export): no voter count rather than a false "0 voters".
  const voters = state ? t_i18n("{count, plural, one {# voter} other {# voters}}", { count: state.voters }) : null;
  const qr = showQr && state?.qr ? <img className="deck-poll-qr" src={state.qr} alt={t_i18n("QR code of the poll link")} /> : null;

  if (block.layout === "all") {
    return (
      // data-fill: a full-slide poll takes the whole slide body (no empty bottom).
      <div className="deck-poll" data-layout="all" data-fill="true" data-show-qr={showQr ? "true" : "false"} {...editAttrs}>
        <div className="deck-poll-join" data-layout="all">
          {qr}
          <div className="deck-poll-join-text">
            <div className="deck-poll-join-label">{t_i18n("Vote on your phone")}</div>
            <JoinLink state={state} />
          </div>
          <div className="deck-poll-meta" data-layout="all">
            {voters ? <span>{voters}</span> : null}
            {state?.locked ? <span className="deck-poll-locked">{t_i18n("Voting closed")}</span> : null}
          </div>
        </div>
        <div className="deck-poll-grid" data-count={block.questions.length}>
          {block.questions.map((spec, index) => {
            const question = questionOf(spec);
            return (
              // A div, never a section: reveal.js forces `display: block !important` on every
              // section under `.slides`, which would void the cell's flex column (gap, fill).
              <div key={spec.id} className="deck-poll-cell">
                <div className="deck-poll-prompt" data-layout="all">
                  <InlineMarkup value={spec.prompt} field={`questions.${index}.prompt`} />
                </div>
                <Results question={question} showResults={showResults} afterLock={afterLock} revealed={revealedOf(question)} />
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  const specQuestion = block.questions.find((q) => q.id === activeId) ?? block.questions[0];
  const question = specQuestion ? questionOf(specQuestion) : undefined;
  const index = block.questions.findIndex((q) => q.id === question?.id);

  return (
    <div className="deck-poll" data-show-qr={showQr ? "true" : "false"} {...editAttrs}>
      <div className="deck-poll-main">
        <div className="deck-poll-meta">
          <span>{t_i18n("Question {current} of {count}", { current: index + 1, count: block.questions.length })}</span>
          {voters ? <span>{voters}</span> : null}
          {state?.locked ? <span className="deck-poll-locked">{t_i18n("Voting closed")}</span> : null}
        </div>
        <div className="deck-poll-prompt">
          {specQuestion ? <InlineMarkup value={specQuestion.prompt} field={`questions.${index}.prompt`} /> : null}
        </div>
        {question ? (
          <Results question={question} showResults={showResults} afterLock={afterLock} revealed={revealedOf(question)} />
        ) : null}
      </div>
      <div className="deck-poll-join">
        <div className="deck-poll-join-label">{t_i18n("Vote on your phone")}</div>
        <JoinLink state={state} />
        {qr}
      </div>
    </div>
  );
}
