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

import { CircleAlert, Play, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";
import type { InterruptedTurn } from "@/types";

interface InterruptedTurnNoticeProps {
  turn: InterruptedTurn;
  /** Start the turn that picks the work up; omitted where the reader cannot send. */
  onResume?: () => void;
  /** Stop showing the notice; omitted where the reader is not the owner. */
  onDismiss?: () => void;
  /** A send would be refused right now (AI off, tools still loading). */
  resumeDisabled?: boolean;
}

/**
 * The end of a transcript whose last turn stopped before its answer:
 * its process stopped (a restart, a deploy, a crash) or it was closed as
 * interrupted. Read from the conversation (`interrupted_turn`), so it is there
 * after a reload, a restart and on every device, until the next turn starts.
 */
export function InterruptedTurnNotice({ turn, onResume: onResumeClick, onDismiss, resumeDisabled = false }: InterruptedTurnNoticeProps) {
  const { t_i18n, intl } = useFormatter();
  // Queued for its automatic resume: it resumes by itself, so no
  // Resume click races the platform's own resume.
  const resumingByItself = turn.auto_resume === true;
  const onResume = resumingByItself ? undefined : onResumeClick;
  const when = turn.interrupted_at ?? turn.started_at;
  const at = when ? intl.formatDate(when, { dateStyle: "medium", timeStyle: "short" }) : null;
  return (
    <div
      role="status"
      data-testid="interrupted-turn-notice"
      className="ml-11 max-w-2xl rounded-sm border border-feedback-alert-primary/25 bg-feedback-alert-primary/[0.04] px-4 py-3"
    >
      <div className="flex items-start gap-3">
        <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-feedback-warning-primary" aria-hidden />
        <div className="min-w-0 flex-1 space-y-1">
          <p className="content-compact font-medium text-default-primary">
            {t_i18n("This response stopped before it finished")}
          </p>
          <p className="content-caption text-default-secondary">
            {turn.interrupted_at
              ? at
                ? t_i18n("It was interrupted on {time}: the platform restarted or stopped the process running it.", { time: at })
                : t_i18n("It was interrupted: the platform restarted or stopped the process running it.")
              : at
                ? t_i18n("The process running it, started on {time}, stopped without finishing it.", { time: at })
                : t_i18n("The process running it stopped without finishing it.")}{" "}
            {turn.tool_calls_kept > 0
              ? t_i18n(
                  "{count, plural, one {Its completed tool call is kept: resuming continues from it instead of running it again.} other {Its # completed tool calls are kept: resuming continues from them instead of running them again.}}",
                  { count: turn.tool_calls_kept },
                )
              : t_i18n("Resuming asks the assistant to pick your request up again.")}
          </p>
          {resumingByItself && (
            <p data-testid="interrupted-turn-auto-resume" className="content-caption font-medium text-default-primary">
              {t_i18n("It is resuming by itself: no need to click anything.")}
            </p>
          )}
          {(onResume || onDismiss) && (
            <div className="flex flex-wrap items-center gap-2 pt-1.5">
              {onResume && (
                <Button size="sm" onClick={onResume} disabled={resumeDisabled}>
                  <Play className="h-3.5 w-3.5" aria-hidden />
                  {t_i18n("Resume")}
                </Button>
              )}
              {onDismiss && (
                <Button size="sm" variant="ghost" onClick={onDismiss}>
                  <X className="h-3.5 w-3.5" aria-hidden />
                  {t_i18n("Dismiss")}
                </Button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
