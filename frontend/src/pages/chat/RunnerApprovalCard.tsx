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
 * Inline approval of a held runner command, on the shared `DecisionCard`
 * (#842, S8; dev-docs/human-gates.md section 3).
 *
 * When the machine's policy holds a shell command for a human decision and the
 * turn was sent from XTM One Desktop, the agent's tool emits a
 * `runner_approval_required` progress frame and WAITS. This card is that
 * decision, anchored above the composer where the person is already looking:
 * what the agent wants to run, on which machine, and the four answers the
 * runner's permission model already knows - approve once, approve and always
 * allow (the exact command joins the machine's shell allow-list), reject once,
 * reject and always block. The two standing answers ask for a confirm first.
 * The answers call the existing `POST /runners/commands/{id}/approve|reject`
 * routes: the runner keeps its own transport, only the card is shared.
 *
 * The countdown shows how long the agent keeps waiting; past it the command
 * stays queued on the Desktop page and the agent tells the user so.
 */
import { useEffect, useState } from "react";

import { DecisionCard, type DecisionAnswer, type DecisionOption, type DecisionView } from "@/components/decisions/DecisionCard";
import { useToast } from "@/components/ui/toast";
import { useApproveRunnerCommand, useRejectRunnerCommand } from "@/hooks/api/useRunnerApi";
import { useFormatter } from "@/i18n";
import { isNotFoundError } from "@/lib/api";
import { RUNNER_COMMAND_KIND_LABELS } from "@/lib/constants";
import { formatCountdown } from "@/lib/utils";
import type { RunnerApprovalRequest } from "@/types";

type Translate = ReturnType<typeof useFormatter>["t_i18n"];

/** The runner's four answers as option keys: what each one sends. */
const RUNNER_ANSWERS: Record<string, { approved: boolean; remember: boolean }> = {
  approve: { approved: true, remember: false },
  always_allow: { approved: true, remember: true },
  reject: { approved: false, remember: false },
  always_block: { approved: false, remember: true },
};

/** The options of a held command: the standing ones only for a shell command. Pure: tests read it. */
export function runnerApprovalOptions(request: RunnerApprovalRequest, t_i18n: Translate): DecisionOption[] {
  const isShell = request.kind === "run_shell";
  const name = request.runner_name || t_i18n("this machine");
  const options: DecisionOption[] = [
    { key: "approve", label: t_i18n("Approve"), effect: t_i18n("Runs this command once. The machine asks again next time.") },
  ];
  if (isShell) {
    options.push({
      key: "always_allow",
      label: t_i18n("Always allow"),
      effect: t_i18n("Runs it now and adds this exact command to the allow-list of {name}.", { name }),
      confirm: t_i18n("This exact command will then run on {name} without asking, until you remove it from the machine's allow-list.", { name }),
      warning: true,
    });
  }
  options.push({ key: "reject", label: t_i18n("Reject"), effect: t_i18n("Does not run it. The agent is told and can adapt.") });
  if (isShell) {
    options.push({
      key: "always_block",
      label: t_i18n("Always block"),
      effect: t_i18n("Does not run it and adds this exact command to the block-list of {name}.", { name }),
      confirm: t_i18n("This exact command will then be refused on {name} without asking, until you remove it from the machine's block-list.", { name }),
      warning: true,
    });
  }
  return options;
}

/** A held command as the card reads it. Pure: tests read it. */
function runnerApprovalView(request: RunnerApprovalRequest, remainingS: number, t_i18n: Translate): DecisionView {
  const kindLabel = t_i18n(RUNNER_COMMAND_KIND_LABELS[request.kind] ?? request.kind);
  return {
    question: t_i18n("Run this {kind} on {name}?", {
      kind: kindLabel.toLowerCase(),
      name: request.runner_name || t_i18n("this machine"),
    }),
    options: runnerApprovalOptions(request, t_i18n),
    freeText: "none",
    notice: (
      <div className="space-y-1.5">
        <pre
          className="overflow-x-auto rounded-sm bg-elevation-default px-2.5 py-1.5 font-mono-plex content-compact text-default-primary"
          data-testid="runner-approval-command"
        >
          <code className="whitespace-pre-wrap break-all">{request.summary || kindLabel}</code>
        </pre>
        <p className="content-caption tabular-nums text-default-secondary" data-testid="runner-approval-countdown">
          {t_i18n("The agent waits {time} for your decision.", { time: formatCountdown(remainingS) })}
        </p>
      </div>
    ),
  };
}

export function RunnerApprovalCard({
  request,
  onDecided,
}: {
  request: RunnerApprovalRequest;
  /** Called once a decision was sent (or the wait ran out) so the host drops the card. */
  onDecided: (commandId: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const approve = useApproveRunnerCommand();
  const reject = useRejectRunnerCommand();
  const busy = approve.isPending || reject.isPending;

  // Countdown to the moment the agent's tool stops waiting. Ticks locally;
  // the backend sends no per-second frames for a held command.
  const deadlineMs = request.received_at_ms + request.expires_in_seconds * 1000;
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    setNowMs(Date.now());
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [request.command_id]);
  const remainingS = Math.max(0, (deadlineMs - nowMs) / 1000);
  useEffect(() => {
    // The agent has moved on: the card would now decide a command whose
    // output can no longer land in this turn. Drop it; the Desktop page keeps
    // the command in its approval list.
    if (remainingS <= 0) onDecided(request.command_id);
  }, [remainingS, onDecided, request.command_id]);

  const decide = (answer: DecisionAnswer) => {
    const sent = answer.option ? RUNNER_ANSWERS[answer.option] : undefined;
    if (!sent) return;
    const mutation = sent.approved ? approve : reject;
    mutation.mutate(
      { commandId: request.command_id, remember: sent.remember },
      {
        onSuccess: () => onDecided(request.command_id),
        onError: (e) => {
          // A 404 means somebody (the Desktop page, another tab) decided
          // first; the resolution frame will drop the card. Anything else is
          // worth a word.
          if (isNotFoundError(e)) return;
          toast({ title: e.message || t_i18n("The decision could not be sent"), variant: "error" });
        },
      },
    );
  };

  return (
    <div
      data-testid="runner-approval-card"
      role="group"
      aria-label={t_i18n("Approve a command on {name}", { name: request.runner_name })}
      className="mx-4 mb-2 animate-fade-in"
    >
      <DecisionCard
        view={runnerApprovalView(request, remainingS, t_i18n)}
        canAnswer
        pending={busy}
        testId="runner-decision"
        onAnswer={decide}
      />
    </div>
  );
}
