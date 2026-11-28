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
 * One card for every human gate (#842, S3; dev-docs/human-gates.md section 3
 * and J.1). A source-neutral `DecisionView` (question, context, options,
 * recommendation, deadline, links, who asks, where from) renders the same
 * way whether a work item, a run's question, a tool approval or an
 * irreversible consent asks it:
 *
 * - the question as the heading, who asks and the sources under it, the
 *   context as Markdown;
 * - every option a full-width tile of the same width, the label on line 1 and
 *   its effect on line 2, the recommended one accented with a pill;
 * - an option that needs a second look (Approve always, a consent, a reject
 *   that asks why) opens a confirm panel under the tiles before it is sent;
 * - free text folded behind "Answer differently" when there are options, open
 *   when there are none;
 * - what only agents read under a folded "Technical details".
 *
 * The card holds no transport: the caller maps its source to a view and sends
 * the answer (`onAnswer`) through the endpoint it already uses.
 */
import { useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Icon, Textarea } from "@filigran/design-system";

import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { Label } from "@/components/ui/label";
import { useFormatter } from "@/i18n";
import { formatShortDateTimeIntl } from "@/lib/relative-time";
import { cn } from "@/lib/utils";

/** One choice of a decision: `key` is what the answer sends. */
export interface DecisionOption {
  key: string;
  label: string;
  effect?: string | null;
  /** A second look before it is sent: the text of the confirm panel. */
  confirm?: string;
  /** The confirm panel takes an optional reason (a reject: the agent reads why). */
  asksReason?: boolean;
  /** The confirm panel reads as a warning (an irreversible action, a standing grant). */
  warning?: boolean;
}

/** A decision as a person reads it, whatever asks it. */
export interface DecisionView {
  question: string;
  /** The question is Markdown (a run's `request_human_input`): rendered, never shown raw. */
  questionMarkdown?: boolean;
  /** Plain lines for the reader, one paragraph each (Markdown). */
  context?: readonly string[];
  /** The whole context when the card's cap cut it (`ask.context_full`, #2800): folded under "Full context". */
  fullContext?: string | null;
  options: readonly DecisionOption[];
  /** The key of the recommended option. */
  recommendation?: string | null;
  /** ISO-8601: shown in the reader's time zone. */
  deadline?: string | null;
  links?: readonly { label: string; url: string }[];
  /** Who asks, named only when the reader may see it. */
  asker?: string | null;
  /**
   * Free text: "auto" folds it behind the options when there are some; "open" shows it unfolded
   * under them (a page opened to write an answer); "none" offers none.
   */
  freeText?: "auto" | "open" | "none";
  freeTextLabel?: string;
  /** Under a folded "Technical details": what only agents read. */
  technical?: ReactNode;
  /** Under the context, before the options (an incomplete card says so here). */
  notice?: ReactNode;
  /** The primary way to the object the decision is about, under the question, before the sources (#2370). */
  lead?: ReactNode;
  /**
   * An answer is recorded and the decision stays open on purpose (a work item's keep-waiting option, #2500):
   * the card says what was answered, by whom and when, and what it still waits for; the options fold
   * behind "Change the answer".
   */
  answered?: DecisionAnswered | null;
}

/** A recorded answer that keeps its decision open (#2500). */
export interface DecisionAnswered {
  label: string;
  /** Who answered, named as the server stored it; null when unknown. */
  by?: string | null;
  /** ISO-8601: shown in the reader's time zone. */
  at: string;
  /** What the decision still waits for, one sentence. */
  waiting: string;
}

/**
 * The effect as the card shows it: a leading "Recommended:" an agent wrote
 * into the text is dropped, the tile carries its own pill (#842, J.1).
 */
export function effectText(effect: string | null | undefined): string {
  return (effect ?? "").replace(/^\s*(?:\(recommended\)|recommended\s*[:.-])\s*/i, "").trim();
}

/**
 * The options in display order: the recommended one first, the others in the
 * order the source gave them.
 */
export function orderedOptions(options: readonly DecisionOption[], recommended?: string | null): DecisionOption[] {
  return [...options].sort((a, b) => Number(b.key === recommended) - Number(a.key === recommended));
}

/**
 * One option as a full-width tile (J.1): the label on line 1, its effect under
 * it, the whole tile one button. The effect is clamped to 3 lines; "Show more"
 * shows only when the clamp actually cuts the text (measured on the rendered
 * span and followed on resize), under the tile, outside the button.
 */
function DecisionTile({
  option,
  recommended,
  disabled,
  selected,
  testId,
  onPick,
}: {
  option: DecisionOption;
  recommended: boolean;
  disabled: boolean;
  selected: boolean;
  testId: string;
  onPick: () => void;
}) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const [cut, setCut] = useState(false);
  const effect = effectText(option.effect);
  const effectId = useId();
  const effectRef = useRef<HTMLSpanElement>(null);
  // Measured while clamped only: once open, the span is whole and the last
  // measure keeps "Show less" in place.
  useLayoutEffect(() => {
    const span = effectRef.current;
    if (!span || open) return;
    const measure = () => setCut(span.scrollHeight > span.clientHeight + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(span);
    return () => observer.disconnect();
  }, [effect, open]);
  const clamped = cut && !open;
  return (
    <li
      className="min-w-0"
      data-testid={`${testId}-tile`}
      data-option={option.key}
      data-recommended={recommended ? "true" : undefined}
    >
      <button
        type="button"
        disabled={disabled}
        onClick={onPick}
        aria-describedby={effect ? effectId : undefined}
        aria-pressed={option.confirm || option.asksReason ? selected : undefined}
        data-testid={`${testId}-option`}
        className={cn(
          "flex w-full flex-col items-stretch gap-1 rounded-sm border px-3 py-2 text-left transition-colors",
          "focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
          "disabled:cursor-not-allowed disabled:opacity-60",
          recommended || selected
            ? "border-accent bg-accent/10 enabled:hover:bg-accent/15"
            : "border-elevation-subtle bg-elevation-default enabled:hover:bg-elevation-hover",
        )}
      >
        <span className="flex min-w-0 items-start gap-2">
          <span className="min-w-0 flex-1 break-words content-base font-semibold text-default-primary" data-testid={`${testId}-option-label`}>
            {option.label}
          </span>
          {recommended && (
            <Badge variant="outline" className="content-caption shrink-0 gap-1 border-accent text-accent" data-testid={`${testId}-recommended`}>
              <Icon name="sparkles" size={16} aria-hidden />
              {t_i18n("Recommended")}
            </Badge>
          )}
        </span>
        {effect && (
          <span
            id={effectId}
            ref={effectRef}
            className={cn("block break-words content-compact text-default-secondary", !open && "line-clamp-3")}
            data-testid={`${testId}-effect`}
            data-clamped={clamped ? "true" : undefined}
          >
            {effect}
          </span>
        )}
      </button>
      {(cut || open) && (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="mt-1 rounded-sm px-1 content-compact text-accent hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
        >
          {open ? t_i18n("Show less") : t_i18n("Show more")}
        </button>
      )}
    </li>
  );
}

export interface DecisionAnswer {
  option?: string;
  text?: string;
}

export function DecisionCard({
  view,
  canAnswer,
  pending = false,
  onAnswer,
  readOnlyNote,
  testId = "decision",
  children,
  textActions,
}: {
  view: DecisionView;
  canAnswer: boolean;
  /** An answer is on its way: every control is disabled. */
  pending?: boolean;
  onAnswer: (answer: DecisionAnswer) => void;
  /** Shown instead of the free text when the reader may not answer. */
  readOnlyNote?: string;
  /** The test id prefix of every part (a source keeps its own: work-item-decision). */
  testId?: string;
  /** What the source adds at the foot of the card (Ask the agent). */
  children?: ReactNode;
  /** Secondary actions in the free text's action row, before "Send answer" (Discard). */
  textActions?: ReactNode;
}) {
  const { t_i18n, intl } = useFormatter();
  const [text, setText] = useState("");
  const [chosen, setChosen] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const textId = useId();
  const options = orderedOptions(view.options, view.recommendation);
  const confirming = options.find((o) => o.key === chosen) ?? null;
  const disabled = !canAnswer || pending;
  const freeTextMode = view.freeText ?? "auto";
  const textLabel = view.freeTextLabel ?? (options.length > 0 ? t_i18n("Or answer in your own words") : t_i18n("Your answer"));
  const context = (view.context ?? []).map((line) => line.trim()).filter(Boolean);
  const fullContext = (view.fullContext ?? "").split("\n").map((line) => line.trim()).filter(Boolean);
  const [fullContextOpen, setFullContextOpen] = useState(false);
  const links = view.links ?? [];

  const pick = (option: DecisionOption) => {
    if (option.confirm || option.asksReason) {
      setChosen((current) => (current === option.key ? null : option.key));
      setReason("");
      return;
    }
    // Words typed under "Answer differently" go with the option.
    onAnswer({ option: option.key, text: text.trim() || undefined });
  };

  const sendText = () => {
    const words = text.trim();
    if (!words) return;
    onAnswer({ text: words });
    setText("");
  };

  const freeText = (
    <div className="space-y-1.5">
      <Label htmlFor={textId} className="text-default-secondary">
        {textLabel}
      </Label>
      <Textarea id={textId} aria-label={textLabel} value={text} rows={2} onChange={(e) => setText(e.target.value)} />
      <div className="flex items-center justify-end gap-2">
        {textActions}
        <Button variant={options.length > 0 ? "outline" : "default"} className="gap-1" disabled={!text.trim() || pending} onClick={sendText}>
          <Icon name="send" size={16} aria-hidden /> {t_i18n("Send answer")}
        </Button>
      </div>
    </div>
  );

  const controls = (
    <>
      {options.length > 0 && (
        <ul className="grid grid-cols-1 gap-2" data-testid={`${testId}-options`}>
          {options.map((option) => (
            <DecisionTile
              key={option.key}
              option={option}
              recommended={!!view.recommendation && option.key === view.recommendation}
              selected={chosen === option.key}
              disabled={disabled}
              testId={testId}
              onPick={() => pick(option)}
            />
          ))}
        </ul>
      )}

      {confirming && (
        <div
          className={cn(
            "space-y-2 rounded-sm border p-3",
            confirming.warning ? "border-feedback-warning-primary/40 bg-feedback-warning-primary/5" : "border-elevation-subtle bg-elevation-default",
          )}
          data-testid={`${testId}-confirm`}
          data-option={confirming.key}
        >
          {confirming.confirm && (
            <p className={cn("flex items-start gap-1.5 content-compact", confirming.warning ? "text-feedback-warning-tertiary" : "text-default-secondary")}>
              {confirming.warning && <Icon name="triangle-alert" size={16} className="mt-0.5 shrink-0" aria-hidden />}
              {confirming.confirm}
            </p>
          )}
          {confirming.asksReason && (
            <div className="space-y-1.5">
              <Label htmlFor={`${textId}-reason`} className="text-default-secondary">
                {t_i18n("Why not? (optional: the agent sees this and can adapt)")}
              </Label>
              <Textarea
                id={`${textId}-reason`}
                aria-label={t_i18n("Why not? (optional: the agent sees this and can adapt)")}
                value={reason} rows={2} onChange={(e) => setReason(e.target.value)} />
            </div>
          )}
          <div className="flex items-center justify-end gap-2">
            <Button variant="ghost" size="sm" className="h-9" disabled={pending} onClick={() => setChosen(null)}>
              {t_i18n("Back")}
            </Button>
            <Button
              size="sm"
              className="h-9"
              disabled={disabled}
              data-testid={`${testId}-confirm-send`}
              onClick={() => onAnswer({ option: confirming.key, text: reason.trim() || undefined })}
            >
              {t_i18n("Confirm: {option}", { option: confirming.label })}
            </Button>
          </div>
        </div>
      )}

      {canAnswer &&
        (freeTextMode === "none" ? null : options.length > 0 && freeTextMode === "auto" ? (
          // With options, free text is the exception: folded behind one line (J.1).
          <details data-testid={`${testId}-free-text`}>
            <DisclosureSummary>{t_i18n("Answer differently")}</DisclosureSummary>
            <div className="pt-2">{freeText}</div>
          </details>
        ) : (
          freeText
        ))}
    </>
  );

  // A card shown read only never says "your decision" (UX r1 B2 of #1497); an answered card waits on, not for you (#2500).
  const header = view.answered
    ? t_i18n("Answer recorded")
    : canAnswer
      ? t_i18n("Waiting for your decision")
      : t_i18n("Waiting for a decision");

  return (
    <section
      className="space-y-3 rounded-sm border border-accent/40 bg-accent/5 p-3"
      aria-labelledby={`${textId}-question`}
      data-testid={testId}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Icon name="message-square" size={16} className="shrink-0 text-accent" aria-hidden />
        <span className="content-caption font-medium uppercase tracking-wider text-default-secondary">
          {header}
        </span>
        {view.deadline && (
          <Badge variant="secondary" className="content-caption ml-auto gap-1" data-testid={`${testId}-deadline`}>
            <Icon name="calendar" size={16} aria-hidden />
            {t_i18n("Answer by {date}", { date: formatShortDateTimeIntl(intl, view.deadline) })}
          </Badge>
        )}
      </div>
      {view.questionMarkdown ? (
        <div id={`${textId}-question`} className="content-base font-medium text-default-primary break-words" data-testid={`${testId}-question`}>
          <ChatMarkdown content={view.question} className="chat-markdown" remoteImages={false} />
        </div>
      ) : (
        <p id={`${textId}-question`} className="content-base font-medium text-default-primary whitespace-pre-wrap break-words" data-testid={`${testId}-question`}>
          {view.question}
        </p>
      )}
      {view.asker && (
        <p className="content-caption text-default-secondary" data-testid={`${testId}-asker`}>
          {t_i18n("Asked by {agent}", { agent: view.asker })}
        </p>
      )}
      {view.lead}
      {links.length > 0 && (
        <ul className="flex flex-wrap gap-2" aria-label={t_i18n("Sources")} data-testid={`${testId}-links`}>
          {links.map((link) => (
            <li key={link.url}>
              <Button asChild size="sm" variant="outline" className="gap-1">
                <a href={link.url} target="_blank" rel="noopener noreferrer" data-testid={`${testId}-link`}>
                  <Icon name="external-link" size={16} className="shrink-0" aria-hidden />
                  {link.label}
                </a>
              </Button>
            </li>
          ))}
        </ul>
      )}
      {context.length > 0 && !(fullContext.length > 0 && fullContextOpen) && (
        // The open fold holds the whole text: the capped copy steps aside so nothing shows twice (#2800).
        <div data-testid={`${testId}-context`}>
          <ChatMarkdown content={context.join("\n\n")} className="chat-markdown content-compact text-default-secondary" remoteImages={false} />
        </div>
      )}
      {fullContext.length > 0 && (
        // The card's cap cut the context: the whole text is one click away, never lost (#2800).
        <details
          data-testid={`${testId}-full-context`}
          open={fullContextOpen}
          onToggle={(e) => setFullContextOpen(e.currentTarget.open)}
        >
          <DisclosureSummary>{t_i18n("Full context")}</DisclosureSummary>
          <div className="pt-2">
            <ChatMarkdown content={fullContext.join("\n\n")} className="chat-markdown content-compact text-default-secondary" remoteImages={false} />
          </div>
        </details>
      )}
      {view.notice}

      {view.answered && (
        <div className="space-y-1 rounded-sm border border-elevation-subtle bg-elevation-default p-3" data-testid={`${testId}-answered`}>
          <p className="flex items-start gap-1.5 content-compact font-medium text-default-primary">
            <Icon name="circle-check" size={16} className="mt-0.5 shrink-0 text-accent" aria-hidden />
            <span className="min-w-0 break-words" data-testid={`${testId}-answered-line`}>
              {view.answered.by
                ? t_i18n("Answered: {option} by {person}, {date}.", {
                    option: view.answered.label,
                    person: view.answered.by,
                    date: formatShortDateTimeIntl(intl, view.answered.at),
                  })
                : t_i18n("Answered: {option}, {date}.", {
                    option: view.answered.label,
                    date: formatShortDateTimeIntl(intl, view.answered.at),
                  })}
            </span>
          </p>
          <p className="content-compact text-default-secondary" data-testid={`${testId}-still-waiting`}>
            {view.answered.waiting}
          </p>
        </div>
      )}

      {view.answered ? (
        // A recorded answer that keeps the decision open (#2500): the options are folded, one click away.
        canAnswer && (
          <details data-testid={`${testId}-change`}>
            <DisclosureSummary>{t_i18n("Change the answer")}</DisclosureSummary>
            <div className="space-y-3 pt-2">{controls}</div>
          </details>
        )
      ) : (
        controls
      )}

      {!canAnswer && readOnlyNote && <p className="content-compact text-default-secondary">{readOnlyNote}</p>}

      {view.technical && (
        <details data-testid={`${testId}-technical`}>
          <DisclosureSummary>{t_i18n("Technical details")}</DisclosureSummary>
          <div className="pt-2 content-compact text-default-secondary">{view.technical}</div>
        </details>
      )}

      {children}
    </section>
  );
}
