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
 * The audience page of a live poll: `/v/CODE` (xtm-one #10157).
 *
 * Opened from the short link or the QR on a slide, on a phone, without an
 * account. It reads ONE poll by its code (`GET /api/v1/public/polls/{code}`,
 * every 2 s) and shows the question the presenter opened, or, for a poll with
 * `layout: "all"` (xtm-one #10235), every question on one scrolling page,
 * answered in any order with one Vote per question; a vote is one
 * `POST .../votes` carrying a random token this browser keeps, so a device
 * answers each question once and nobody is identified. Plain `fetch` on
 * purpose, as the public deck page: no session token, no redirect to login.
 */
import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, Lock, Vote } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useFormatter } from "@/i18n";
import { API_BASE } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { PollQuestionState, PollState } from "@/types";
import { Spinner } from "@filigran/design-system";

const VOTER_KEY = "xtm-poll-voter";
const VOTED_KEY = "xtm-poll-voted";

function voterToken(): string {
  try {
    const known = window.localStorage.getItem(VOTER_KEY);
    if (known && known.length >= 16) return known;
    const bytes = new Uint8Array(24);
    window.crypto.getRandomValues(bytes);
    const fresh = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    window.localStorage.setItem(VOTER_KEY, fresh);
    return fresh;
  } catch {
    return `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`.padEnd(16, "0");
  }
}

function votedSet(): Set<string> {
  try {
    return new Set(JSON.parse(window.localStorage.getItem(VOTED_KEY) ?? "[]") as string[]);
  } catch {
    return new Set();
  }
}

function rememberVote(key: string) {
  const known = votedSet();
  known.add(key);
  try {
    window.localStorage.setItem(VOTED_KEY, JSON.stringify(Array.from(known).slice(-200)));
  } catch {
    /* private mode: the server still refuses a second vote */
  }
}

/** One question with its own answer state: the form, the thanks line, the error and the counts. */
function QuestionCard({
  code,
  voter,
  question,
  locked,
  hasVoted,
  headingLevel,
  onVoted,
}: {
  code: string;
  voter: string;
  question: PollQuestionState;
  locked: boolean;
  hasVoted: boolean;
  /** 1 when the page holds this question alone, 2 under the page title of an all-questions poll. */
  headingLevel: 1 | 2;
  onVoted: () => void;
}) {
  const { t_i18n } = useFormatter();
  const [picked, setPicked] = useState<number | null>(null);
  const [words, setWords] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const inputId = `poll-words-${question.id}`;

  const send = async (answer: number | string) => {
    setSending(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/public/polls/${encodeURIComponent(code)}/votes`, {
        method: "POST",
        credentials: "omit",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question_id: question.id, voter, answer }),
      });
      if (res.ok) {
        rememberVote(`${code}:${question.id}`);
        onVoted();
      } else {
        const body = (await res.json().catch(() => null)) as { detail?: { message?: string } } | null;
        setError(body?.detail?.message ?? t_i18n("Your vote could not be sent. Try again."));
      }
    } catch {
      setError(t_i18n("Your vote could not be sent. Try again."));
    } finally {
      setSending(false);
    }
  };

  const results = question.results;
  const Heading = headingLevel === 1 ? "h1" : "h2";
  return (
    <section className="flex flex-col gap-4" aria-live="polite" data-question={question.id}>
      <Heading className={cn("font-semibold leading-tight", headingLevel === 1 ? "text-2xl" : "text-xl")}>
        {question.prompt}
      </Heading>
      {locked ? (
        <p className="flex items-center gap-2 text-sm text-default-secondary">
          <Lock className="size-4" aria-hidden />
          {t_i18n("Voting is closed for this question.")}
        </p>
      ) : hasVoted ? (
        <p className="flex items-center gap-2 text-sm">
          <CheckCircle2 className="size-4 text-feedback-success-primary" aria-hidden />
          {t_i18n("Thanks, your vote is in. Look at the screen.")}
        </p>
      ) : question.kind === "word_cloud" ? (
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (words.trim()) void send(words.trim());
          }}
        >
          <label id={`${inputId}-label`} className="text-sm font-medium" htmlFor={inputId}>
            {t_i18n("{count, plural, one {One word} other {Up to # words, separated by commas}}", {
              count: question.max_words,
            })}
          </label>
          <Input
            id={inputId}
            aria-labelledby={`${inputId}-label`}
            value={words}
            maxLength={90}
            onChange={(e) => setWords(e.target.value)}
            // The single open question takes the focus; on a page of questions the reader scrolls first.
            autoFocus={headingLevel === 1}
          />
          <Button type="submit" disabled={sending || !words.trim()}>
            {t_i18n("Send")}
          </Button>
        </form>
      ) : (
        <div className="flex flex-col gap-3" role="radiogroup" aria-label={question.prompt}>
          {(question.kind === "rating" ? ["1", "2", "3", "4", "5"] : question.options).map((option, index) => {
            const value = question.kind === "rating" ? index + 1 : index;
            return (
              <button
                key={index}
                type="button"
                role="radio"
                aria-checked={picked === value}
                onClick={() => setPicked(value)}
                className={cn(
                  "min-h-12 rounded-md border px-4 py-3 text-start text-base",
                  picked === value ? "border-accent bg-accent/10 font-medium" : "border-elevation-subtle",
                )}
              >
                {option}
              </button>
            );
          })}
          <Button disabled={sending || picked === null} onClick={() => picked !== null && void send(picked)}>
            {t_i18n("Vote")}
          </Button>
        </div>
      )}
      {error ? (
        <p role="alert" className="text-sm text-feedback-error-primary">
          {error}
        </p>
      ) : null}
      {results && question.kind !== "word_cloud" && results.counts ? (
        <ul className="flex flex-col gap-1 text-sm text-default-secondary">
          {results.counts.map((count, index) => (
            <li key={index} className={cn(question.correct === index && "font-semibold text-feedback-success-primary")}>
              {(question.kind === "rating" ? String(index + 1) : question.options[index]) ?? ""}: {count}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

export default function PollVote() {
  const { t_i18n } = useFormatter();
  const { code = "" } = useParams();
  const [voter] = useState(voterToken);
  const [voted, setVoted] = useState<Set<string>>(votedSet);

  const poll = useQuery<PollState, Error>({
    queryKey: ["public-poll", code],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/public/polls/${encodeURIComponent(code)}`, { credentials: "omit" });
      if (!res.ok) throw new Error(String(res.status));
      return res.json();
    },
    refetchInterval: 2000,
    // The page's own states answer a bad code or a closed poll; the app-wide
    // error boundary ("something went wrong") is not for the audience.
    throwOnError: false,
    retry: (count, err) => err.message !== "404" && count < 3,
  });

  if (poll.isLoading) {
    return (
      <main className="flex min-h-dvh items-center justify-center bg-elevation-default text-default-primary">
        <Spinner size="lg" label={t_i18n("Loading")} />
      </main>
    );
  }
  if (poll.isError || !poll.data) {
    return (
      <main className="flex min-h-dvh flex-col items-center justify-center gap-2 bg-elevation-default p-6 text-center text-default-primary">
        <h1 className="text-lg font-semibold">{t_i18n("This poll is not available")}</h1>
        <p className="text-sm text-default-secondary">{t_i18n("Check the link on the screen and try again.")}</p>
      </main>
    );
  }

  const every = poll.data.layout === "all";
  // An all-questions poll lists every question; a stepped poll holds the open one only.
  const questions = every ? poll.data.questions : poll.data.questions.slice(0, 1);
  const onVoted = () => {
    setVoted(votedSet());
    void poll.refetch();
  };
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-md flex-col gap-6 bg-elevation-default p-5 text-default-primary">
      <header className="flex items-center gap-2 text-sm text-default-secondary">
        <Vote className="size-4" aria-hidden />
        {every ? (
          <h1 className="text-sm font-normal">{t_i18n("Live poll")}</h1>
        ) : (
          <span>{t_i18n("Live poll")}</span>
        )}
      </header>
      {questions.length === 0 ? (
        <p className="text-base">{t_i18n("Waiting for the presenter to open a question.")}</p>
      ) : (
        <div className={cn("flex flex-col", every ? "gap-10" : "gap-6")}>
          {questions.map((question) => (
            <QuestionCard
              // keyed by question: a stepped poll moving on starts a fresh form
              key={question.id}
              code={code}
              voter={voter}
              question={question}
              locked={poll.data.locked}
              hasVoted={voted.has(`${code}:${question.id}`)}
              headingLevel={every ? 2 : 1}
              onVoted={onVoted}
            />
          ))}
        </div>
      )}
    </main>
  );
}
