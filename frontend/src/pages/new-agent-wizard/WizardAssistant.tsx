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
 * The agent wizard's assistant, on screen (AGENT_WIZARD_ASSIST): the prompt
 * bar under the wizard, and the frame that points at the field it is filling.
 *
 * The conversation lives in the bar only; nothing is stored. Each answer is
 * played onto the draft by `useAssistPlayback`, with an undo.
 */
import { useEffect, useRef, useState } from "react";
import { ArrowUp, ChevronDown, ChevronUp, Sparkles, Square, Undo2 } from "lucide-react";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { Spinner } from "@filigran/design-system";

export interface WizardAssistTurn {
  role: "user" | "assistant";
  content: string;
  /** A refusal, or what was left out of an answer: never sent back as
   *  conversation, shown apart. */
  notice?: boolean;
}

/**
 * Points at what the assistant is changing: the active border and an "AI"
 * tag while `active`, scrolled into view. Renders its children untouched
 * otherwise.
 */
export function AssistFrame({
  active,
  children,
  className,
}: {
  active: boolean;
  children: React.ReactNode;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const ref = useRef<HTMLDivElement>(null);
  const reducedMotion = usePrefersReducedMotion();
  useEffect(() => {
    if (active) ref.current?.scrollIntoView({ block: "nearest", behavior: reducedMotion ? "auto" : "smooth" });
  }, [active, reducedMotion]);
  return (
    <div
      ref={ref}
      data-assist-active={active || undefined}
      className={cn(
        "relative rounded-sm transition-shadow duration-300",
        active && "active-border",
        className,
      )}
    >
      {active && (
        <span className="pointer-events-none absolute -top-2.5 right-2 z-10 flex items-center gap-1 rounded-sm border border-transparent bg-accent px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-negative-primary shadow">
          <Sparkles className="h-2.5 w-2.5" /> {t_i18n("AI")}
        </span>
      )}
      {children}
    </div>
  );
}

export function WizardAssistantBar({
  turns,
  pending,
  playing,
  status,
  started,
  disabled,
  canUndo,
  onSend,
  onStop,
  onUndo,
  className,
}: {
  turns: WizardAssistTurn[];
  pending: boolean;
  playing: boolean;
  /** What the assistant is doing right now, when it is. */
  status: string | null;
  started: boolean;
  disabled: boolean;
  canUndo: boolean;
  onSend: (instruction: string) => void;
  onStop: () => void;
  onUndo: () => void;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const [instruction, setInstruction] = useState("");
  const [open, setOpen] = useState(true);
  const listRef = useRef<HTMLOListElement>(null);
  const busy = pending || playing;

  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [turns.length, open]);

  const send = () => {
    const text = instruction.trim();
    if (text.length < 2 || busy || disabled) return;
    onSend(text);
    setInstruction("");
    setOpen(true);
  };

  return (
    <div
      className={cn(
        "rounded-sm border border-accent/30 layer-1 bg-elevation-default",
        busy && "active-border",
        className,
      )}
      data-wizard-assistant
      data-busy={busy || undefined}
    >
      {turns.length > 0 && (
        <div className="border-b border-elevation-subtle-soft">
          <button
            type="button"
            className="flex w-full items-center gap-2 px-3 py-1.5 text-[11px] text-default-secondary hover:text-default-primary"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            <Sparkles className="h-3 w-3 text-accent" />
            <span>{t_i18n("Assistant")}</span>
            <Badge variant="cyan" className="px-1.5 py-0 text-[9px]">{t_i18n("Experimental")}</Badge>
            {open ? <ChevronDown className="ml-auto h-3.5 w-3.5" /> : <ChevronUp className="ml-auto h-3.5 w-3.5" />}
          </button>
          {open && (
            <ol ref={listRef} className="max-h-32 space-y-1.5 overflow-y-auto px-3 pb-2 content-compact">
              {turns.map((turn, i) => (
                <li
                  key={i}
                  className={cn(
                    "whitespace-pre-wrap rounded-sm px-2.5 py-1.5",
                    turn.notice
                      ? "bg-feedback-alert-primary/5 text-feedback-warning-tertiary"
                      : turn.role === "user"
                        ? "ml-10 bg-elevation-hover"
                        : "mr-10 border border-accent/30 bg-accent/5",
                  )}
                >
                  {turn.content}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
      {busy && (
        <p role="status" className="flex items-center gap-2 px-3 pt-2 text-[11px] text-accent">
          <Spinner size="sm" tone="inherit" />
          {pending ? t_i18n("Preparing the changes...") : status}
        </p>
      )}
      <div className="flex items-end gap-2 p-2">
        <Sparkles className="mb-2 ml-1 h-4 w-4 shrink-0 text-accent" />
        <textarea
          aria-label={t_i18n("Ask the assistant to fill in the wizard")}
          value={instruction}
          rows={1}
          maxLength={4000}
          disabled={disabled}
          className="field-sizing-content max-h-28 min-h-[36px] flex-1 resize-none bg-transparent px-1 py-2 content-base placeholder:text-default-secondary focus-visible:outline-hidden disabled:cursor-not-allowed"
          placeholder={
            started
              ? t_i18n("Describe a change, e.g. bind the team mailbox and keep each run under one dollar")
              : t_i18n("Describe the agent you need; the assistant picks a starting point and fills in the steps")
          }
          onChange={(e) => setInstruction(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              send();
            }
          }}
        />
        {canUndo && !busy && (
          <Button variant="ghost" size="sm" className="mb-0.5 gap-1.5 text-default-secondary" onClick={onUndo}>
            <Undo2 className="h-3.5 w-3.5" /> {t_i18n("Undo")}
          </Button>
        )}
        {busy ? (
          <Button size="sm" variant="outline" className="mb-0.5 gap-1.5" onClick={onStop}>
            <Square className="h-3 w-3" /> {t_i18n("Stop")}
          </Button>
        ) : (
          <AiDisabledTip>
            <Button
              size="sm"
              className="mb-0.5 h-8 w-8 p-0"
              aria-label={t_i18n("Send")}
              disabled={disabled || instruction.trim().length < 2}
              onClick={send}
            >
              <ArrowUp className="h-4 w-4" />
            </Button>
          </AiDisabledTip>
        )}
      </div>
    </div>
  );
}
