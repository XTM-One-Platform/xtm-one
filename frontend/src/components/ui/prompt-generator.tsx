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
 * PromptGenerator — AI-assisted prompt/persona generation.
 *
 * Drop this next to any textarea to let users describe what they want
 * in plain language and have the LLM generate the full prompt.
 *
 * Usage:
 *   <PromptGenerator
 *     contextType="persona"
 *     currentText={persona}
 *     onGenerated={setPersona}
 *     agentName="Email Triage"
 *   />
 *
 * A field whose draft needs context the generic `/ai/generate-prompt` cannot
 * see (the slide a review note is about) passes `generate` — an async
 * function from the brief to the text — and keeps the same bar, the same
 * "Improve with AI" path and the same AI-disabled tooltip.
 */

import { useState, useRef, useEffect, useCallback } from "react";
import { Sparkles, Send, X, RefreshCw } from "lucide-react";
import { Spinner } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useGeneratePrompt } from "@/hooks/api/useMiscApi";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ContextType =
  | "persona"
  | "assignment_prompt"
  | "gate_prompt"
  | "system_prompt"
  | "ai_label_prompt"
  | "hitl_prompt"
  | "presentation_comment"
  | "custom_tool_code"
  | "agent_account_instructions"
  | "agent_account_rule_instructions"
  | "fleet_collaboration_instruction";

interface PromptGeneratorProps {
  /** Which type of prompt to generate */
  contextType: ContextType;
  /** Current text in the field (used for "improve" mode) */
  currentText?: string;
  /** Called with the generated text */
  onGenerated: (text: string) => void;
  /** Optional agent name for context */
  agentName?: string;
  /** Optional assignment name for context */
  assignmentName?: string;
  /**
   * A field-specific generator. When set, the brief goes here instead
   * of `/ai/generate-prompt` — the comment composer drafts against the deck
   * and the slide it is on. Resolves to the text to place in the field; the
   * caller reports its own failures (a rejection only ends the pending state).
   */
  generate?: (brief: string) => Promise<string>;
  /** Extra CSS classes on the root wrapper */
  className?: string;
}

// ---------------------------------------------------------------------------
// Placeholder map — one entry per context type
// ---------------------------------------------------------------------------

const PLACEHOLDERS: Record<ContextType, string> = {
  persona: "Describe the agent's role and behavior...",
  assignment_prompt: "Describe what this assignment should do...",
  gate_prompt: "Describe when this assignment should run...",
  system_prompt: "Describe how the assistant should behave...",
  ai_label_prompt: "Describe how AI-generated messages should be labeled...",
  hitl_prompt: "Describe when the AI should ask for human input...",
  presentation_comment: "Say what the note should convey...",
  custom_tool_code: "Describe what the code should do...",
  agent_account_instructions: "Describe how the account speaks and what it must never commit to...",
  agent_account_rule_instructions: "Describe this audience and what it may be told...",
  fleet_collaboration_instruction: "Say when the fleet should call this member and what to ask...",
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function PromptGenerator({
  contextType,
  currentText,
  onGenerated,
  agentName,
  assignmentName,
  generate: customGenerate,
  className,
}: PromptGeneratorProps) {
  const aiOff = useAiActionDisabled();
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const [description, setDescription] = useState("");
  const [customPending, setCustomPending] = useState(false);
  const generate = useGeneratePrompt();
  const inputRef = useRef<HTMLInputElement>(null);
  // The re-entrancy latch. `pending` is state, so it only flips after a
  // render: Enter and a click landing in the same tick, or a double Enter,
  // both read it as false. The ref is set before the request leaves and
  // cleared when it settles, whatever way it settles.
  const inFlight = useRef(false);

  const hasCurrentText = !!currentText?.trim();
  const pending = generate.isPending || customPending;

  // Auto-focus the input when opened
  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const close = useCallback(() => {
    setOpen(false);
    setDescription("");
  }, []);

  // `brief` defaults to the typed description; the refine shortcut passes its
  // own so it never reads a stale (possibly empty) closure.
  const handleGenerate = useCallback(
    (brief: string = description) => {
      const text = brief.trim();
      // One request at a time: the buttons are unmounted and the input is
      // disabled while pending, but Enter from a still-focused input (or a
      // caller's shortcut) must not start a second generation either - and
      // every path in (Enter, the refine shortcut, the Send button) comes
      // through here, so the latch below is the one gate.
      if (!text || pending || inFlight.current) return;
      inFlight.current = true;
      if (customGenerate) {
        setCustomPending(true);
        // Entered through a settled promise so a callback that throws
        // synchronously (its contract is a promise, a bug is not) rejects
        // here and still reaches `finally` - otherwise the latch and the
        // pending state would stay set for the life of the component.
        Promise.resolve()
          .then(() => customGenerate(text))
          .then((generated) => {
            onGenerated(generated);
            close();
          })
          .catch(() => undefined)
          .finally(() => {
            inFlight.current = false;
            setCustomPending(false);
          });
        return;
      }
      generate.mutate(
        {
          description: text,
          context_type: contextType,
          current_text: currentText || undefined,
          agent_name: agentName,
          assignment_name: assignmentName,
        },
        {
          onSuccess: (data) => {
            onGenerated(data.generated_prompt);
            close();
          },
          onSettled: () => {
            inFlight.current = false;
          },
        },
      );
    },
    [
      description,
      pending,
      customGenerate,
      contextType,
      currentText,
      agentName,
      assignmentName,
      generate,
      onGenerated,
      close,
    ],
  );

  const handleRefine = useCallback(() => {
    const brief = "Improve and refine the existing text";
    setDescription(brief);
    handleGenerate(brief);
  }, [handleGenerate]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleGenerate();
      }
      if (e.key === "Escape") close();
    },
    [handleGenerate, close],
  );

  // ── Collapsed state — just a button ──
  if (!open) {
    return (
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className={cn("gap-1.5 content-compact text-accent hover:text-accent/80 h-7 px-2", className)}
        onClick={() => setOpen(true)}
      >
        <Sparkles className="h-3 w-3" />
        {hasCurrentText ? t_i18n("Improve with AI") : t_i18n("Generate with AI")}
      </Button>
    );
  }

  // ── Expanded state — inline input bar ──
  return (
    <div className={cn("flex items-center gap-2 rounded-sm bg-accent/5 px-3 py-2", className)}>
      <Sparkles className="h-3.5 w-3.5 text-accent shrink-0" />
      <input
        ref={inputRef}
        type="text"
        className="flex-1 bg-transparent content-base placeholder:text-default-secondary outline-hidden min-w-0"
        placeholder={t_i18n(PLACEHOLDERS[contextType])}
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={pending || aiOff}
      />
      {pending ? (
        <Spinner size="sm" tone="inherit" className="shrink-0 text-accent" label={t_i18n("Generating...")} />
      ) : (
        <>
          {hasCurrentText && (
            <Tip content={t_i18n("Refine current text")}>
              <button aria-label={t_i18n("Refine current text")}
                type="button"
                className="text-default-secondary hover:text-accent transition-colors"
                onClick={handleRefine}
              >
                <RefreshCw className="h-3.5 w-3.5" />
              </button>
            </Tip>
          )}
          <AiDisabledTip><Tip content={t_i18n("Generate")}>
            <button aria-label={t_i18n("Generate")}
              type="button"
              className="text-accent hover:text-accent/80 transition-colors disabled:opacity-40"
              onClick={() => handleGenerate()}
              disabled={!description.trim() || aiOff}
            >
              <Send className="h-3.5 w-3.5" />
            </button>
          </Tip></AiDisabledTip>
          <Tip content={t_i18n("Cancel")}>
            <button aria-label={t_i18n("Cancel")}
              type="button"
              className="text-default-secondary hover:text-default-primary transition-colors"
              onClick={close}
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </Tip>
        </>
      )}
    </div>
  );
}
