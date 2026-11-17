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
 * The emergency-stop confirmation — the profile menu's "panic button".
 *
 * A stop cancels work that cannot be resumed, so the confirm button stays
 * disabled until the user retypes a random challenge code shown in the
 * dialog: a slip of the mouse on a red menu entry must not be enough. The
 * code is regenerated every time the dialog opens, and compared ignoring
 * case and surrounding whitespace (it is a safeguard, not a password).
 *
 * Administrators choose the scope here; everyone else stops their own
 * activity. The server enforces the same rule (`platform` is 403 for a
 * non-admin) — this dialog only shows what the caller may do.
 *
 * Once the stop has succeeded the dialog turns into a short "why?" form: an
 * emergency must never wait for a justification, so the reason is asked right
 * AFTER and written onto the audit row the stop returned (and onto the
 * assignments it paused). Skipping is allowed; the stop is already done.
 */
import { useEffect, useMemo, useState } from "react";
import { OctagonX, ShieldAlert, User as UserIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { OptionCard } from "@/components/ui/option-card";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { useEmergencyStop, useRecordEmergencyStopReason, type PanicScope } from "@/hooks/api/usePanicApi";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { challengeMatches, generateChallengeCode } from "@/lib/challenge-code";

interface EmergencyStopDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Whether the platform scope is offered. */
  isAdmin: boolean;
}

export function EmergencyStopDialog({ open, onOpenChange, isAdmin }: EmergencyStopDialogProps) {
  const { t_i18n } = useFormatter();
  const evaluation = useFeatureFlag("EVALUATION");
  const { toast } = useToast();
  const stop = useEmergencyStop();
  const recordReason = useRecordEmergencyStopReason();
  const [scope, setScope] = useState<PanicScope>("self");
  const [code, setCode] = useState("");
  const [typed, setTyped] = useState("");
  // "confirm" is the challenge; "reason" is the form shown once the stop is done.
  const [phase, setPhase] = useState<"confirm" | "reason">("confirm");
  const [auditId, setAuditId] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  // A fresh code per opening: a code memorised from a previous dialog must
  // not carry over, and the input starts empty.
  useEffect(() => {
    if (open) {
      setCode(generateChallengeCode());
      setTyped("");
      setScope("self");
      setPhase("confirm");
      setAuditId(null);
      setReason("");
    }
  }, [open]);

  const matches = useMemo(() => challengeMatches(code, typed), [code, typed]);
  const busy = stop.isPending || recordReason.isPending;

  const handleOpenChange = (next: boolean) => {
    if (busy) return;
    onOpenChange(next);
  };

  const saveReason = () => {
    const text = reason.trim();
    if (!auditId || !text || busy) return;
    recordReason.mutate(
      { auditId, reason: text },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Reason recorded"), variant: "success" });
          onOpenChange(false);
        },
      },
    );
  };

  const confirm = () => {
    if (!matches || busy) return;
    stop.mutate(scope, {
      onSuccess: (result) => {
        // ``completed`` is false when cancelling the live work failed after the
        // door was closed: the counts are what was really halted.
        const partial = result.completed === false;
        toast({
          title: partial ? t_i18n("Emergency stop partly applied") : t_i18n("Emergency stop done"),
          description: [
            partial
              ? t_i18n(
                  "Assignments are paused, but some running work could not be cancelled at once; the follow-up sweeps cancelled what they reach. Check the server log.",
                )
              : null,
            t_i18n(
              "{runs, plural, one {# run} other {# runs}}, {tasks, plural, one {# background task} other {# background tasks}}, {chats, plural, one {# chat turn} other {# chat turns}} and {flows, plural, one {# flow run} other {# flow runs}} cancelled; {assignments, plural, one {# assignment} other {# assignments}} paused.",
              {
                runs: result.runs_cancelled,
                tasks: result.sub_agent_tasks_cancelled,
                chats: result.chat_turns_cancelled,
                flows: result.flow_runs_cancelled,
                assignments: result.assignments_paused,
              },
            ),
            result.ai_disabled ? t_i18n("AI has been disabled for the whole platform.") : null,
            result.late_admissions_cancelled > 0
              ? t_i18n("{count, plural, one {# run had been admitted while the stop was being applied and was cancelled} other {# runs had been admitted while the stop was being applied and were cancelled}} by the follow-up sweep.", { count: result.late_admissions_cancelled })
              : null,
          ]
            .filter(Boolean)
            .join(" "),
          variant: partial ? "error" : "success",
          durationMs: 12000,
        });
        // The stop is done. Now — and only now — ask why.
        if (result.audit_id) {
          setAuditId(result.audit_id);
          setPhase("reason");
        } else {
          onOpenChange(false);
        }
      },
      // The global MutationCache handler already surfaces the error toast.
    });
  };

  if (phase === "reason") {
    return (
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="max-w-md" data-testid="emergency-stop-reason-dialog">
          <DialogHeader>
            <div className="flex items-start gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-feedback-success-primary/10">
                <OctagonX className="h-5 w-5 text-feedback-success-primary" />
              </div>
              <div className="space-y-1">
                <DialogTitle className="text-base">{t_i18n("Everything is stopped. Why?")}</DialogTitle>
                <DialogDescription className="content-base leading-relaxed">
                  {t_i18n(
                    "The stop is already done. The reason you give here is recorded in the audit trail next to it and on every assignment it paused.",
                  )}
                </DialogDescription>
              </div>
            </div>
          </DialogHeader>
          <div className="space-y-2 pt-1">
            <label htmlFor="emergency-stop-reason" className="content-compact text-default-secondary">
              {t_i18n("Reason for the emergency stop")}
            </label>
            <textarea
              id="emergency-stop-reason"
              autoFocus
              rows={3}
              disabled={busy}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder={t_i18n("Suspicious agent behaviour, runaway costs, incident in progress…")}
              className="w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base text-default-primary placeholder:text-default-secondary focus:outline-hidden focus-visible:ring-2 focus-visible:ring-focus focus:border-accent disabled:opacity-50"
            />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" size="sm" onClick={() => handleOpenChange(false)} disabled={busy}>
              {t_i18n("Skip")}
            </Button>
            <Button
              size="sm"
              disabled={!reason.trim() || busy}
              onClick={saveReason}
              data-testid="emergency-stop-reason-save"
            >
              {busy ? t_i18n("Saving…") : t_i18n("Save reason")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-md" data-testid="emergency-stop-dialog">
        <DialogHeader>
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-feedback-error-primary/10">
              <OctagonX className="h-5 w-5 text-feedback-error-primary" />
            </div>
            <div className="space-y-1">
              <DialogTitle className="text-base">{t_i18n("Emergency stop")}</DialogTitle>
              <DialogDescription className="content-base leading-relaxed">
                {t_i18n(
                  "Immediately cancel every running agent activity and pause the assignments in the selected scope. Work already in progress is stopped and cannot be resumed.",
                )}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        {isAdmin && (
          <div role="radiogroup" aria-label={t_i18n("Scope")} className="grid gap-2 pt-1">
            <OptionCard
              selected={scope === "self"}
              onSelect={() => setScope("self")}
              icon={UserIcon}
              title={t_i18n("My activity only")}
              description={t_i18n(
                "Everything that runs for you: chats, runs, background tasks, flows, and the assignments you own or that run as you.",
              )}
              disabled={busy}
            />
            <OptionCard
              selected={scope === "platform"}
              onSelect={() => setScope("platform")}
              icon={ShieldAlert}
              title={t_i18n("Whole platform")}
              description={evaluation
                ? t_i18n("Every user's activity, evaluations and autonomous runs. AI is also switched off for everyone until an administrator turns it back on.")
                : t_i18n("Every user's activity and autonomous runs. AI is also switched off for everyone until an administrator turns it back on.")}
              disabled={busy}
              danger
            />
          </div>
        )}

        <div className="space-y-2 pt-1">
          <label htmlFor="emergency-stop-code" className="content-compact text-default-secondary">
            {t_i18n("To confirm, type the code below:")}
          </label>
          <div
            className="select-all rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/5 px-3 py-2 text-center font-mono-plex text-lg font-semibold tracking-[0.35em] text-feedback-error-primary"
            data-testid="emergency-stop-code"
            aria-live="polite"
          >
            {code}
          </div>
          <Input aria-label={t_i18n("To confirm, type the code below:")}
            id="emergency-stop-code"
            autoFocus
            autoComplete="off"
            spellCheck={false}
            placeholder={t_i18n("Type the code to enable the button")}
            value={typed}
            disabled={busy}
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") confirm();
            }}
            className="font-mono-plex uppercase tracking-widest"
          />
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" size="sm" onClick={() => handleOpenChange(false)} disabled={busy}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            size="sm"
            className="bg-feedback-error-primary hover:bg-feedback-error-primary/90 text-negative-primary border-0"
            disabled={!matches || busy}
            onClick={confirm}
            data-testid="emergency-stop-confirm"
          >
            {busy ? t_i18n("Stopping…") : t_i18n("Stop everything")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
