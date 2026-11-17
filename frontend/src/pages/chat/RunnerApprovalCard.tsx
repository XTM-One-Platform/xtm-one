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
 * Inline approval of a held runner command.
 *
 * When the machine's policy holds a shell command for a human decision and the
 * turn was sent from XTM One Desktop, the agent's tool emits a
 * `runner_approval_required` progress frame and WAITS. This card is that
 * decision, anchored above the composer where the person is already looking:
 * what the agent wants to run, on which machine, and the four answers the
 * runner's permission model already knows - approve once, approve and always
 * allow (the exact command joins the machine's shell allow-list), reject once,
 * reject and always block. The buttons call the existing
 * `POST /runners/commands/{id}/approve|reject` routes; nothing here is a new
 * permission path.
 *
 * A countdown shows how long the agent keeps waiting; past it the command
 * stays queued on the Desktop page and the agent tells the user so.
 */
import { useEffect, useState } from "react";
import { Check, ShieldAlert, Terminal, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useApproveRunnerCommand, useRejectRunnerCommand } from "@/hooks/api/useRunnerApi";
import { useToast } from "@/components/ui/toast";
import { isNotFoundError } from "@/lib/api";
import { RUNNER_COMMAND_KIND_LABELS } from "@/lib/constants";
import { formatCountdown } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { RunnerApprovalRequest } from "@/types";
import { Spinner } from "@filigran/design-system";

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

  const decide = (approved: boolean, remember: boolean) => {
    const mutation = approved ? approve : reject;
    mutation.mutate(
      { commandId: request.command_id, remember },
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

  const kindLabel = t_i18n(RUNNER_COMMAND_KIND_LABELS[request.kind] ?? request.kind);
  const isShell = request.kind === "run_shell";

  return (
    <div
      data-testid="runner-approval-card"
      role="group"
      aria-label={t_i18n("Approve a command on {name}", { name: request.runner_name })}
      className="mx-4 mb-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.06] p-3 animate-fade-in"
    >
      <div className="flex items-start gap-2.5">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-sm bg-feedback-alert-primary/15">
          <ShieldAlert className="h-4 w-4 text-feedback-warning-primary" />
        </div>
        <div className="min-w-0 flex-1 space-y-1.5">
          <p className="content-compact text-default-secondary">
            {t_i18n("The agent wants to run a {kind} on {name}. It is waiting for your decision.", {
              kind: kindLabel.toLowerCase(),
              name: request.runner_name || t_i18n("this machine"),
            })}
          </p>
          <pre className="flex items-start gap-2 overflow-x-auto rounded-sm bg-elevation-default px-2.5 py-1.5 font-mono-plex content-compact text-default-primary">
            {isShell && <Terminal className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden />}
            <code className="whitespace-pre-wrap break-all">{request.summary || kindLabel}</code>
          </pre>
          {/* The refusals read in the error tone at 4.5:1 or better on the amber card in both themes:
              `error-tertiary` (the light red in dark, the deep red in light); `error-primary` falls to
              4.31:1 on the amber fill over a dark card, and any faded form lower. */}
          <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
            <Button size="sm" className="h-7 gap-1.5 px-2.5 content-compact" disabled={busy} onClick={() => decide(true, false)} aria-busy={approve.isPending || undefined}>
              {approve.isPending && <Spinner size="sm" tone="inherit" />}{!approve.isPending && <Check className="h-3.5 w-3.5" />}
              {t_i18n("Approve")}
            </Button>
            {isShell && (
              <Button size="sm" variant="outline" className="h-7 px-2.5 content-compact" disabled={busy} onClick={() => decide(true, true)}>
                {t_i18n("Always allow")}
              </Button>
            )}
            <Button size="sm" variant="ghost" className="h-7 gap-1.5 px-2.5 content-compact text-feedback-error-tertiary hover:text-feedback-error-tertiary" data-tone="error" disabled={busy} onClick={() => decide(false, false)} aria-busy={reject.isPending || undefined}>
              {reject.isPending && <Spinner size="sm" tone="inherit" />}{!reject.isPending && <X className="h-3.5 w-3.5" />}
              {t_i18n("Reject")}
            </Button>
            {isShell && (
              <Button size="sm" variant="ghost" className="h-7 px-2.5 content-compact text-feedback-error-tertiary hover:text-feedback-error-tertiary" data-tone="error" disabled={busy} onClick={() => decide(false, true)}>
                {t_i18n("Always block")}
              </Button>
            )}
            <span className="ml-auto text-[11px] tabular-nums text-default-secondary">
              {t_i18n("Waiting {time}", { time: formatCountdown(remainingS) })}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
