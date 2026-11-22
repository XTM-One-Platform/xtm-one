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
 * A run waiting for a person, on the shared `DecisionCard` (#842, S3;
 * dev-docs/human-gates.md section 3). Two kinds of pause map to it:
 *
 * - a tool approval: Approve, Approve always, Reject. One verdict decides
 *   every pending call, as the decision router does (`decision_router.py`);
 *   Approve always asks for a confirm (its reach outlives the run) and keeps
 *   the call fingerprint the backend checks; Reject asks why. A call that
 *   needs the person's own consent (`requires_consent`) offers no Approve
 *   always and its Approve asks for a confirm. The tool and its arguments are
 *   under "Technical details".
 * - a question (`request_human_input`): the question, free text, and, when the
 *   question asks for `APPROVE <code>` (an irreversible action escalated by
 *   `hitl_consent.py`), one explicit consent option that sends exactly that
 *   reply, so the server check stays the one that decides.
 *
 * The answer goes through the endpoint each caller already uses.
 */
import { Icon } from "@filigran/design-system";

import { DecisionCard, type DecisionAnswer, type DecisionOption, type DecisionView } from "@/components/decisions/DecisionCard";
import { ProposalArguments } from "@/components/ui/action-proposal";
import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";
import { toolDisplayName } from "@/lib/constants";
import { approvalCodesIn, approvalReply } from "@/lib/hitl-approval";
import type { ActionProposal, ApprovalDecision, ApprovalVerdict } from "@/types";

type Translate = ReturnType<typeof useFormatter>["t_i18n"];

/** The option key of the explicit consent to an escalated irreversible action. */
export const CONSENT_OPTION = "consent";

const VERDICTS: readonly ApprovalVerdict[] = ["approve", "approve_always", "reject"];

/** The options of a tool approval (human-gates.md section 2). Pure: tests read it. */
export function approvalOptions(proposals: readonly ActionProposal[], t_i18n: Translate): DecisionOption[] {
  const consent = proposals.some((p) => p.requires_consent);
  const options: DecisionOption[] = [
    {
      key: "approve",
      label: t_i18n("Approve"),
      effect:
        proposals.length === 1
          ? t_i18n("Runs this call once. The tool asks again next time.")
          : t_i18n("{count, plural, one {Runs # call once.} other {Runs these # calls once.}} The tool asks again next time.", { count: proposals.length }),
      ...(consent
        ? {
            confirm: t_i18n("This needs your own consent: the action is run as you approve it now, and it cannot be undone."),
            warning: true,
          }
        : {}),
    },
  ];
  // A consent is given call by call: no standing approval answers it.
  if (!consent) {
    options.push({
      key: "approve_always",
      label: t_i18n("Approve always"),
      effect: t_i18n("Runs it now and stops asking you for this tool, scheduled runs included, until you revoke it in your profile."),
      confirm: t_i18n(
        "“Approve always” saves a preference for you. That tool will then run without asking, including on scheduled runs nobody is watching, until you revoke it in your profile.",
      ),
      warning: true,
    });
  }
  options.push({
    key: "reject",
    label: t_i18n("Reject"),
    effect: t_i18n("Does not run it. The agent is told, with your reason, and can adapt."),
    asksReason: true,
  });
  return options;
}

/**
 * The decisions a verdict sends: one per pending call, the reason on a
 * reject. Null for an option that is not a verdict. Pure: tests read it.
 */
export function approvalDecisions(
  proposals: readonly ActionProposal[],
  answer: DecisionAnswer,
): ApprovalDecision[] | null {
  const verdict = VERDICTS.find((v) => v === answer.option);
  if (!verdict) return null;
  return proposals.map((p) => ({
    tool_call_id: p.tool_call_id,
    decision: verdict,
    ...(verdict === "reject" && answer.text ? { rejection_reason: answer.text } : {}),
  }));
}

/** A tool approval as the card reads it. Pure: tests read it. */
export function approvalDecisionView(proposals: readonly ActionProposal[], t_i18n: Translate): DecisionView {
  const names = proposals.map((p) => toolDisplayName(p.tool_name, t_i18n));
  return {
    // The tool names are progress labels ("Generating file"): they go on their
    // own line under the question, never inside it (UX r1 B2 on #1045).
    question:
      proposals.length === 1
        ? t_i18n("Approve this action?")
        : t_i18n("{count, plural, one {Approve # action?} other {Approve these # actions?}}", { count: proposals.length }),
    context: [
      names.join(", "),
      ...(proposals.length === 1 && proposals[0].tool_description ? [proposals[0].tool_description] : []),
    ],
    options: approvalOptions(proposals, t_i18n),
    freeText: "none",
    technical: (
      <div className="space-y-3" data-testid="run-decision-calls">
        {proposals.map((p) => (
          <div key={p.tool_call_id} className="space-y-1">
            <p className="font-mono-plex text-[11px] text-default-secondary">{p.tool_name}</p>
            <ProposalArguments proposal={p} />
          </div>
        ))}
      </div>
    ),
  };
}

/** A run's question as the card reads it. Pure: tests read it. */
export function questionDecisionView(question: string, t_i18n: Translate): DecisionView {
  const codes = approvalCodesIn(question);
  const options: DecisionOption[] =
    codes.length > 0
      ? [
          {
            key: CONSENT_OPTION,
            label:
              codes.length === 1
                ? t_i18n("Approve {code}", { code: codes[0] })
                : t_i18n("Approve all ({count})", { count: codes.length }),
            effect: t_i18n("Replies {reply} and resumes the run", { reply: approvalReply(codes) }),
            confirm: t_i18n("This action cannot be undone. Your reply is checked against the code before anything runs."),
            warning: true,
          },
        ]
      : [];
  return {
    question,
    questionMarkdown: true,
    options,
    freeTextLabel: options.length > 0 ? t_i18n("Or answer in your own words") : t_i18n("Your answer"),
  };
}

export function RunDecisionCard({
  runId,
  question,
  proposals,
  canAnswer,
  pending,
  onApprove,
  onRespond,
  onDismiss,
}: {
  runId: string;
  question?: string | null;
  /** The pending tool calls: a tool approval when there is at least one. */
  proposals: readonly ActionProposal[];
  canAnswer: boolean;
  pending?: boolean;
  onApprove?: (runId: string, decisions: ApprovalDecision[]) => void;
  onRespond?: (runId: string, response: string) => void;
  onDismiss?: (runId: string) => void;
}) {
  const { t_i18n } = useFormatter();
  if (proposals.length > 0) {
    return (
      <DecisionCard
        view={approvalDecisionView(proposals, t_i18n)}
        canAnswer={canAnswer && !!onApprove}
        pending={pending}
        testId="run-decision"
        readOnlyNote={t_i18n("Waiting for approval from someone with access to this run.")}
        onAnswer={(answer) => {
          const decisions = approvalDecisions(proposals, answer);
          if (decisions && onApprove) onApprove(runId, decisions);
        }}
      />
    );
  }
  const text = (question ?? "").trim();
  if (!text) return null;
  const view = questionDecisionView(text, t_i18n);
  return (
    <DecisionCard
      view={view}
      canAnswer={canAnswer && !!onRespond}
      pending={pending}
      testId="run-decision"
      readOnlyNote={t_i18n("Only admins can respond to company-managed agent requests.")}
      onAnswer={(answer) => {
        if (!onRespond) return;
        if (answer.option === CONSENT_OPTION) {
          onRespond(runId, approvalReply(approvalCodesIn(text)));
          return;
        }
        if (answer.text) onRespond(runId, answer.text);
      }}
      textActions={
        canAnswer && onDismiss ? (
          // Same row and height as "Send answer", Discard first (UX r1 B3 on #1045).
          <Button
            variant="ghost"
            className="h-9 gap-1.5 text-default-secondary hover:text-feedback-error-primary"
            disabled={pending}
            onClick={() => onDismiss(runId)}
            data-testid="run-decision-discard"
          >
            <Icon name="x" size={16} aria-hidden />
            {t_i18n("Discard")}
          </Button>
        ) : undefined
      }
    />
  );
}
