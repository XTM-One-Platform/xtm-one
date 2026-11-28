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
 * The page an answer link opens - `/decide/:token` (dev-docs/human-gates.md
 * I.4). A card sent from a Microsoft account, an agent account or an email
 * carries one link per option; the link is signed for one recipient and one
 * option. Signed in as that recipient, opening an option link answers in one
 * click (no second confirmation) and the page shows the recorded answer; the
 * "Answer with text" link shows the question and a text field. A second open,
 * or an open after another surface answered, shows the decided state (who,
 * which option, when) and answers nothing. A link sent to someone else, an
 * expired link and a link that is not valid each say so, with nothing to act
 * on. The link to the source sits under every state the reader may act from.
 *
 * An open decision is drawn by the shared `DecisionCard` (one component for
 * every human gate, #842 S3): the question, the context as Markdown, the
 * deadline, the options with the recommended one, and the free text open on
 * the "Answer with text" link. Only the states proper to a link (recording,
 * decided, refused) are drawn here.
 */
import { useEffect, useRef } from "react";
import { Link, useParams } from "react-router-dom";
import { Icon, Spinner } from "@filigran/design-system";

import { DecisionCard, type DecisionView } from "@/components/decisions/DecisionCard";
import { Button } from "@/components/ui/button";
import { askOptions } from "@/components/work-items/WorkItemDecisionCard";
import {
  TEXT_LINK_OPTION,
  linkErrorStatus,
  useAnswerDecisionLink,
  useDecisionLink,
  type DecisionLinkView,
} from "@/hooks/api/useDecisionLinkApi";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { formatShortDateTimeIntl } from "@/lib/relative-time";

type Refusal = "elsewhere" | "invalid" | "expired" | "failed";
type Translate = ReturnType<typeof useFormatter>["t_i18n"];

/** An open link as the shared card reads it. */
function linkDecisionView(view: DecisionLinkView, t_i18n: Translate): DecisionView {
  const ask = view.ask ?? {};
  return {
    question: ask.question?.trim() || t_i18n("An agent waits for your decision."),
    context: (ask.context ?? "").split("\n"),
    fullContext: ask.context_full ?? null,
    // An option picked on the text link's card asks to confirm it like the work item card (#2150).
    options: askOptions(ask.options ?? [], t_i18n),
    recommendation: ask.recommendation ?? null,
    deadline: view.deadline ?? ask.deadline ?? null,
    links: ask.links ?? [],
    freeText: view.option === TEXT_LINK_OPTION ? "open" : "none",
  };
}

function refusalOf(error: unknown): Refusal {
  const status = linkErrorStatus(error);
  if (status === 403) return "elsewhere";
  if (status === 404) return "invalid";
  if (status === 410) return "expired";
  return "failed";
}

/**
 * The frame every state of the page shares: one centred card, the source link under it.
 * `card`: the children are the shared decision card, which draws its own border.
 */
function Frame({ children, sourceLink, card = false }: { children: React.ReactNode; sourceLink?: string | null; card?: boolean }) {
  const { t_i18n } = useFormatter();
  return (
    <main className="flex min-h-dvh items-center justify-center bg-elevation-default px-4 py-8 text-default-primary">
      <div className="w-full max-w-lg space-y-3">
        {card ? (
          <div data-testid="decision-link">{children}</div>
        ) : (
          <section
            className="space-y-3 rounded-sm border border-elevation-subtle bg-elevation-default p-4"
            data-testid="decision-link"
          >
            {children}
          </section>
        )}
        {sourceLink && (
          <div className="flex justify-center">
            <Button asChild variant="link" className="gap-1" data-testid="decision-link-source">
              <Link to={sourceLink}>
                <Icon name="external-link" size={16} className="shrink-0" aria-hidden />
                {t_i18n("Open the source")}
              </Link>
            </Button>
          </div>
        )}
      </div>
    </main>
  );
}

/** The question the decision asked, as a heading. */
function Question({ view }: { view: DecisionLinkView }) {
  const { t_i18n } = useFormatter();
  const question = view.ask?.question?.trim() || t_i18n("An agent waits for your decision.");
  return (
    <h1 className="content-base font-medium text-default-primary" data-testid="decision-link-question">
      {question}
    </h1>
  );
}

/** Who answered, which option, when; `now` when this click recorded it. */
function Decided({ view, now }: { view: DecisionLinkView; now: boolean }) {
  const { t_i18n, intl } = useFormatter();
  const decided = view.decided;
  const cancelled = decided?.status === "cancelled" || view.status === "cancelled";
  const answer = decided?.label || decided?.option || decided?.text?.trim() || null;
  const when = decided?.answered_at ? formatShortDateTimeIntl(intl, decided.answered_at) : null;
  return (
    <>
      <div className="flex items-center gap-2">
        <Icon
          name={cancelled ? "ban" : "circle-check"}
          size={20}
          className={cancelled ? "shrink-0 text-default-secondary" : "shrink-0 text-feedback-success-primary"}
          aria-hidden
        />
        <span className="content-base font-medium" data-testid="decision-link-state" data-state={now ? "answered" : cancelled ? "closed" : "decided"}>
          {now
            ? t_i18n("Your answer is recorded.")
            : cancelled
              ? t_i18n("This decision is closed: nobody needs to answer it any more.")
              : t_i18n("This decision is already answered.")}
        </span>
      </div>
      <Question view={view} />
      {!cancelled && answer && (
        <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 content-compact" data-testid="decision-link-decided">
          <dt className="text-default-secondary">{t_i18n("Answer given")}</dt>
          <dd className="min-w-0 break-words font-medium">{answer}</dd>
          {decided?.answered_by && (
            <>
              <dt className="text-default-secondary">{t_i18n("Answered by")}</dt>
              <dd className="min-w-0 break-words">{decided.answered_by}</dd>
            </>
          )}
          {when && (
            <>
              <dt className="text-default-secondary">{t_i18n("Answered at")}</dt>
              <dd>{when}</dd>
            </>
          )}
        </dl>
      )}
      {!now && !cancelled && (
        <p className="content-compact text-default-secondary">{t_i18n("Opening the link again changes nothing.")}</p>
      )}
    </>
  );
}

/** A link that answers nothing: sent to someone else, expired, not valid, or unreadable. */
function Refused({ refusal, onRetry }: { refusal: Refusal; onRetry: () => void }) {
  const { t_i18n } = useFormatter();
  const copy: Record<Refusal, { title: string; body: string }> = {
    elsewhere: {
      title: t_i18n("This answer link was sent to someone else."),
      body: t_i18n("Only the person it was sent to can answer with it. Your answer was not recorded."),
    },
    expired: {
      title: t_i18n("This answer link has expired."),
      body: t_i18n("Answer from the platform instead, or ask for the decision to be sent again."),
    },
    invalid: {
      title: t_i18n("This answer link is not valid."),
      body: t_i18n("It may be incomplete or the decision no longer exists. Open the original message again."),
    },
    failed: {
      title: t_i18n("The decision could not be loaded."),
      body: t_i18n("Check your connection and try again."),
    },
  };
  return (
    <>
      <div className="flex items-center gap-2">
        <Icon name="triangle-alert" size={20} className="shrink-0 text-feedback-warning-primary" aria-hidden />
        <h1 className="content-base font-medium" data-testid="decision-link-state" data-state={refusal}>
          {copy[refusal].title}
        </h1>
      </div>
      <p className="content-compact text-default-secondary">{copy[refusal].body}</p>
      {refusal === "failed" && (
        <div>
          <Button variant="outline" size="sm" className="gap-1.5" onClick={onRetry}>
            <Icon name="refresh-cw" size={16} aria-hidden /> {t_i18n("Try again")}
          </Button>
        </div>
      )}
    </>
  );
}

export default function DecisionLink() {
  const { token = "" } = useParams<{ token: string }>();
  const { t_i18n } = useFormatter();
  const link = useDecisionLink(token);
  const answer = useAnswerDecisionLink(token);
  // The answer route is AI-gated (FR-018): with AI off the link reads the
  // decision and says why it cannot answer, it never clicks into a refusal.
  const aiOff = useAiActionDisabled();
  const aiReason = useAiDisabledReason();
  // One answer per page load: React's double effect in development and a
  // refetch never send the option twice (the server is single use anyway).
  const sent = useRef(false);

  const view = link.data;
  const isOptionLink = !!view && view.option !== TEXT_LINK_OPTION;
  const shouldAnswer = !!view && view.status === "open" && isOptionLink && !aiOff;

  useEffect(() => {
    if (!shouldAnswer || sent.current) return;
    sent.current = true;
    answer.mutate({});
  }, [shouldAnswer, answer]);

  if (link.isLoading) {
    return (
      <Frame>
        <Spinner size="md" label={t_i18n("Loading...")} />
      </Frame>
    );
  }

  if (link.isError || !view) {
    return (
      <Frame>
        <Refused refusal={refusalOf(link.error)} onRetry={() => void link.refetch()} />
      </Frame>
    );
  }

  if (answer.isError) {
    return (
      <Frame sourceLink={view.source_link}>
        <Refused
          refusal={refusalOf(answer.error)}
          onRetry={() => {
            answer.reset();
            sent.current = false;
            void link.refetch();
          }}
        />
      </Frame>
    );
  }

  if (answer.data && answer.data.status !== "open") {
    return (
      <Frame sourceLink={answer.data.source_link}>
        <Decided view={answer.data} now={!!answer.data.answered_now} />
      </Frame>
    );
  }

  if (view.status !== "open") {
    return (
      <Frame sourceLink={view.source_link}>
        <Decided view={view} now={false} />
      </Frame>
    );
  }

  const recording = (isOptionLink && !aiOff) || answer.isPending;
  const option = isOptionLink ? view.option_label || view.option : null;

  return (
    <Frame sourceLink={view.source_link} card>
      <DecisionCard
        view={linkDecisionView(view, t_i18n)}
        canAnswer={!aiOff}
        readOnlyNote={aiReason ?? undefined}
        pending={recording}
        onAnswer={({ option: picked, text }) => answer.mutate({ option: picked, text })}
        testId="decision-link-card"
      >
        {recording && (
          <div className="flex items-center gap-2" data-testid="decision-link-state" data-state="recording">
            <Spinner size="sm" label={option ? t_i18n("Recording your answer: {option}", { option }) : t_i18n("Recording your answer")} />
            <span className="content-compact text-default-secondary">
              {option ? t_i18n("Recording your answer: {option}", { option }) : t_i18n("Recording your answer")}
            </span>
          </div>
        )}
      </DecisionCard>
    </Frame>
  );
}
