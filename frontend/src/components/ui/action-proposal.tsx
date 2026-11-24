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
 * Structured approval prompt for a gated tool call.
 *
 * Renders what the agent wants to do — the tool, what it does, and every
 * argument with its own description — and collects a decision per call.
 *
 * The argument descriptions matter more than they look. A reviewer shown
 * `cascade: true` cannot judge it; shown "cascade — also delete linked
 * entities" they can. So each field is labelled from the tool's own
 * `input_schema` rather than from the argument name alone.
 */

import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { toolDisplayName } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { WARNING_ON_HOVER_TEXT } from "@/lib/warning-text";
import type { ActionProposal, ApprovalDecision, ApprovalVerdict } from "@/types";

interface ArgumentSchema {
  description?: string;
  type?: string;
}

/**
 * Scrolls *ref* into view once *active* turns true, and again on each later
 * transition into it.
 *
 * A paused turn renders at the bottom of a thread that does not scroll itself,
 * and every control here is revealed rather than always present: the prompt
 * arrives below the fold, "No" opens a reason box under the card, "Yes, always"
 * opens a disclosure under the list. In each case the click looks like it did
 * nothing and the decision stalls, which on a *paused* turn stalls the agent
 * too. rAF because the revealed node must be laid out before it can be scrolled
 * to.
 */
function useRevealIntoView<T extends HTMLElement>(active: boolean) {
  const ref = useRef<T>(null);
  useEffect(() => {
    if (!active) return;
    const raf = requestAnimationFrame(() => {
      ref.current?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
        inline: "nearest",
      });
    });
    return () => cancelAnimationFrame(raf);
  }, [active]);
  return ref;
}

/** Pull `{ description, type }` for one argument out of a JSON Schema. */
function argumentSchema(
  inputSchema: Record<string, unknown>,
  name: string,
): ArgumentSchema {
  const properties = inputSchema?.properties as
    | Record<string, ArgumentSchema>
    | undefined;
  return properties?.[name] ?? {};
}

function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2) ?? "";
}

/**
 * Every argument of a gated call, each labelled from the tool's own
 * `input_schema`. Shared with the decision card's "Technical details" (#842, S3).
 */
export function ProposalArguments({ proposal }: { proposal: ActionProposal }) {
  const argumentNames = Object.keys(proposal.arguments ?? {});
  if (argumentNames.length === 0) return null;
  return (
    <div className="space-y-1.5">
      {argumentNames.map((name) => {
        const schema = argumentSchema(proposal.input_schema ?? {}, name);
        return (
          <div key={name} className="text-[11px]">
            <div className="flex items-baseline gap-1.5">
              <span className="font-mono-plex text-default-secondary">{name}</span>
              {schema.description && <span className="text-default-disabled">— {schema.description}</span>}
            </div>
            <pre className="mt-0.5 whitespace-pre-wrap break-all font-mono-plex text-default-secondary">
              {formatValue(proposal.arguments?.[name])}
            </pre>
          </div>
        );
      })}
    </div>
  );
}

interface ActionProposalCardProps {
  proposal: ActionProposal;
  decision: ApprovalDecision | undefined;
  onDecide: (decision: ApprovalDecision) => void;
  disabled?: boolean;
  /** The conversation moved on before this was answered (FR-002c4).
   *  Read-only: no verdict can be given any more, and the backend refuses one
   *  with 409. Kept on screen rather than removed because a question the
   *  product asked, and a gated call that never ran, should leave a trace. */
  expired?: boolean;
}

export function ActionProposalCard({
  proposal,
  decision,
  onDecide,
  disabled,
  expired,
}: ActionProposalCardProps) {
  const { t_i18n } = useFormatter();
  // Rejecting asks why before it sends. That reason is the agent's only signal
  // to correct itself — with argument editing gone, a reject IS the correction
  // channel — and an empty one reaches it as "No reason given.", which tells it
  // nothing. Optional, so a reviewer who has nothing to add just confirms.
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");

  const rejectPanelRef = useRevealIntoView<HTMLDivElement>(rejecting);

  const verdict = decision?.decision;

  const decide = (next: ApprovalVerdict) => {
    onDecide({
      tool_call_id: proposal.tool_call_id,
      decision: next,
      ...(next === "reject" && reason.trim()
        ? { rejection_reason: reason.trim() }
        : {}),
    });
  };

  return (
    <div
      className={cn(
        "rounded-sm border p-3 space-y-2",
        expired
          ? "border-elevation-subtle-soft bg-elevation-hover"
          : "border-feedback-warning-primary/20 bg-feedback-warning-primary/5",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p
            className={cn(
              "content-compact font-medium truncate",
              // On the amber /5 wash of the box: the tertiary, in both themes (warning wash rule).
              expired ? "text-default-secondary" : "text-feedback-warning-tertiary",
            )}
          >
            {/* The action as a person reads it, never the internal tool id (UX r1 B3 of #10208). */}
            {toolDisplayName(proposal.tool_name, t_i18n)}
          </p>
          {proposal.tool_description && (
            <p className="text-[11px] text-default-secondary mt-0.5">
              {proposal.tool_description}
            </p>
          )}
        </div>
        {expired && (
          <span className="shrink-0 rounded-sm bg-elevation-hover px-1.5 py-0.5 content-caption font-medium text-default-secondary">
            {t_i18n("Expired")}
          </span>
        )}
        {!expired && verdict && (
          <span
            className={cn(
              "shrink-0 rounded-sm px-1.5 py-0.5 content-caption font-medium",
              verdict === "reject"
                ? "bg-feedback-error-primary/15 text-feedback-error-primary"
                : "bg-feedback-success-primary/15 text-feedback-success-primary",
            )}
          >
            {verdict === "reject"
              ? t_i18n("Rejected")
              : verdict === "approve_always"
                ? t_i18n("Always allowed")
                : t_i18n("Approved")}
          </span>
        )}
      </div>

      <ProposalArguments proposal={proposal} />

      {!expired && !verdict && rejecting && (
        <div ref={rejectPanelRef} className="space-y-1.5 pt-1">
          <label className="text-[11px] text-default-secondary">
            {t_i18n("Why not? (optional: the agent sees this and can adapt)")}
          </label>
          <textarea
            autoFocus
            rows={2}
            className="w-full rounded-sm border border-elevation-subtle-soft bg-elevation-default px-2 py-1 text-[11px]"
            placeholder={t_i18n("e.g. wrong environment: use staging instead")}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <div className="flex items-center gap-1.5">
            <Button
              size="sm"
              className="h-9 content-compact"
              disabled={disabled}
              onClick={() => decide("reject")}
            >
              <X className="size-3 mr-1" />
              {t_i18n("Decline this call")}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-9 content-compact"
              disabled={disabled}
              onClick={() => {
                setRejecting(false);
                setReason("");
              }}
            >
              {t_i18n("Back")}
            </Button>
          </div>
        </div>
      )}

      {!expired && !verdict && !rejecting && (
        <div className="flex flex-wrap items-center gap-1.5 pt-1">
          <Button
            size="sm"
            variant="ghost"
            className="h-9 content-compact text-feedback-error-primary hover:text-feedback-error-primary"
            disabled={disabled}
            onClick={() => setRejecting(true)}
          >
            <X className="size-3 mr-1" />
            {t_i18n("No")}
          </Button>
          <Button
            size="sm"
            className="h-9 content-compact"
            disabled={disabled}
            onClick={() => decide("approve")}
          >
            <Check className="size-3 mr-1" />
            {t_i18n("Yes")}
          </Button>
          {/* A hint before the click; the disclosure that actually governs
              this decision is the one revealed after it, which cannot be
              missed and has to be read before confirming. A consent is given
              call by call: no "always" to offer. */}
          {!proposal.requires_consent && (
            <Tip
              content={t_i18n(
                "Also applies to this agent's scheduled runs, until you revoke it",
              )}
            >
              <Button
                size="sm"
                variant="outline"
                className="h-9 content-compact"
                disabled={disabled}
                onClick={() => decide("approve_always")}
              >
                {t_i18n("Yes, always")}
              </Button>
            </Tip>
          )}
        </div>
      )}
    </div>
  );
}

interface ActionProposalListProps {
  proposals: ActionProposal[];
  onSubmit?: (decisions: ApprovalDecision[]) => void;
  isSubmitting?: boolean;
  /** Where this is rendered. Only affects wording — a conversation has no run
   *  to continue, so "Continue run" would be describing something else. */
  surface?: "run" | "chat";
  /** These were never answered and no longer can be: the conversation moved on
   *  (FR-002c4). Renders read-only, with no submit and no reveal — the
   *  set is a record, not a prompt, so scrolling the viewport to it would fight
   *  the user for a decision they can no longer make. */
  expired?: boolean;
  /** Truthy when a submit attempt failed. The list latches itself busy on
   *  submit so a double-fire cannot answer a resuming turn twice — but a failed
   *  attempt resumed nothing, and without this the reviewer is left with dead
   *  buttons and a toast, unable to retry without a page reload.
   *
   *  The latch is released by this value *changing*, so it must differ between
   *  consecutive failures. A react-query `isError` does that on its own — it
   *  passes through false while the retry is pending. A caller with no such
   *  cycle passes a count of failed attempts instead: 0 is falsy and means none, and every failure after that
   *  is a new value. Passing a bare `true` twice would leave the second retry
   *  dead, which is the bug this exists to prevent. */
  submitFailed?: boolean | number;
}

/**
 * Collects a decision for every proposal in a paused turn.
 *
 * Submission is all-or-nothing because the backend requires it: resuming with
 * an undecided call would leave its `tool_use` block without a `tool_result`,
 * which the LLM providers reject outright. Hence the counter, and a submit that
 * waits for the whole set rather than a per-card "go".
 *
 * That constraint is satisfied the moment the last card is decided, so the set
 * submits itself there. A verdict is final once clicked — a decided card shows a
 * badge, not buttons — so a separate confirm would gate work already committed
 * to without offering any way to revise it.
 *
 * "Always" is the exception, and the only click that earns a confirm: its reach
 * outlives the turn and has to be read before it is committed (FR-015c /
 * SC-010). Any set containing one waits for the button.
 */
export function ActionProposalList({
  proposals,
  onSubmit,
  isSubmitting,
  surface = "run",
  expired,
  submitFailed,
}: ActionProposalListProps) {
  const { t_i18n } = useFormatter();
  const [decisions, setDecisions] = useState<Record<string, ApprovalDecision>>({});
  // Guards the auto-submit path against a double fire: React may re-render
  // between the verdict and the parent flipping `isSubmitting`, and submitting
  // the same decisions twice would answer a turn that is already resuming.
  const submitted = useRef(false);
  // The same fact as state, because the ref cannot re-render. Without it the
  // footer button stayed enabled after an auto-submit — reading as the step
  // still to take, while `submit` silently refused it on the ref. A live parent
  // hides this list as soon as the turn resumes, so it only showed when
  // something went wrong: exactly when a dead button is most misleading.
  const [sent, setSent] = useState(false);
  const busy = isSubmitting || sent;

  // Nothing was answered, so the latch has to come off — both halves of it, or
  // the button re-enables and `submit` still refuses on the ref.
  useEffect(() => {
    if (!submitFailed) return;
    submitted.current = false;
    setSent(false);
  }, [submitFailed]);

  const submit = (all: ApprovalDecision[]) => {
    if (expired || !onSubmit) return;
    if (submitted.current || isSubmitting) return;
    submitted.current = true;
    setSent(true);
    onSubmit(all);
  };

  const decided = proposals.filter((p) => decisions[p.tool_call_id]).length;
  const allDecided = decided === proposals.length && proposals.length > 0;
  const willRunAlways = Object.values(decisions).some(
    (d) => d.decision === "approve_always",
  );

  // The disclosure and the confirm button appear *below* the cards, so the
  // "always" click can push them past the fold (FR-015c / SC-010: its reach has
  // to be read before it is confirmed).
  const footerRef = useRevealIntoView<HTMLDivElement>(willRunAlways);
  // The prompt itself arrives at the bottom of a thread that has just grown by
  // a whole turn, so it renders below the fold on a short viewport. Always true:
  // the mount *is* the reveal.
  const rootRef = useRevealIntoView<HTMLDivElement>(!expired);

  return (
    <div
      ref={rootRef}
      className="space-y-2"
      onClick={(e) => e.stopPropagation()}
    >
      <p
        className={cn(
          "content-compact font-medium",
          expired ? "text-default-secondary" : WARNING_ON_HOVER_TEXT,
        )}
      >
        {expired
          ? proposals.length === 1
            ? t_i18n(
                "This tool needed your approval and was never run. The conversation moved on:",
              )
            : t_i18n(
                "These tools needed your approval and were never run. The conversation moved on:",
              )
          : proposals.length === 1
            ? t_i18n("The agent wants to run a tool that needs your approval:")
            : t_i18n("The agent wants to run tools that need your approval:")}
      </p>

      {proposals.map((proposal) => (
        <ActionProposalCard
          key={proposal.tool_call_id}
          proposal={proposal}
          decision={decisions[proposal.tool_call_id]}
          disabled={busy}
          expired={expired}
          onDecide={(decision) => {
            const next = { ...decisions, [decision.tool_call_id]: decision };
            setDecisions(next);
            // Safe to read `decisions` from the closure: each verdict is its own
            // click, so this handler always sees the state the previous one set.
            const all = proposals.map((p) => next[p.tool_call_id]);
            if (all.every(Boolean) && !all.some((d) => d.decision === "approve_always")) {
              submit(all);
            }
          }}
        />
      ))}

      {!expired && willRunAlways && (
        <p className={cn("flex items-start gap-1.5 content-caption", WARNING_ON_HOVER_TEXT)}>
          <AlertTriangle className="size-3 mt-0.5 shrink-0" />
          {t_i18n(
            "“Yes, always” saves a preference for you. That tool will then run without asking, including on scheduled runs nobody is watching, until you revoke it in your profile.",
          )}
        </p>
      )}

      {/* No footer on an expired set: there is no decision left to count and
          nothing to submit. A disabled Confirm would read as a step still to
          take. */}
      {!expired && (
      <div
        ref={footerRef}
        className="flex items-center justify-between gap-2 pt-1"
      >
        {/* The counter earns its place only when there is more than one thing to
            count; on a single proposal it reads as a progress bar for a
            one-step process. */}
        <span className="text-[11px] text-default-secondary">
          {proposals.length > 1
            ? t_i18n("{decided} of {total} decided")
                .replace("{decided}", String(decided))
                .replace("{total}", String(proposals.length))
            : ""}
        </span>
        <Button
          size="sm"
          className="h-9 content-compact"
          disabled={!allDecided || busy}
          onClick={() => submit(proposals.map((p) => decisions[p.tool_call_id]))}
        >
          {busy
            ? t_i18n("Submitting…")
            : surface === "chat"
              ? t_i18n("Confirm")
              : t_i18n("Continue run")}
        </Button>
      </div>
      )}
    </div>
  );
}
