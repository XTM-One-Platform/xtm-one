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
 * Lifting an emergency stop. Unlike the stop, this is not an emergency, so
 * the reason is asked BEFORE acting and is mandatory: the server records it
 * on the `panic_resume` audit row and on every assignment it re-enables.
 */
import { useEffect, useState } from "react";
import { Icon } from "@filigran/design-system";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { useEmergencyResume, type PanicScope } from "@/hooks/api/usePanicApi";

interface EmergencyResumeDialogProps {
  /** `null` keeps the dialog closed. */
  scope: PanicScope | null;
  onClose: () => void;
}

export function EmergencyResumeDialog({ scope, onClose }: EmergencyResumeDialogProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const resume = useEmergencyResume();
  const [reason, setReason] = useState("");
  const open = scope !== null;
  const busy = resume.isPending;
  const canSubmit = reason.trim().length > 0 && !busy;

  useEffect(() => {
    if (open) setReason("");
  }, [open]);

  const submit = () => {
    if (!canSubmit || scope === null) return;
    resume.mutate(
      { scope, reason: reason.trim() },
      {
        onSuccess: (result) => {
          toast({
            title: t_i18n("Emergency stop lifted"),
            description: [
              t_i18n("{count} assignments resumed.", { count: result.assignments_resumed }),
              result.ai_enabled ? t_i18n("AI has been switched back on.") : null,
            ]
              .filter(Boolean)
              .join(" "),
            variant: "success",
          });
          onClose();
        },
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !busy) onClose(); }}>
      <DialogContent className="max-w-md" data-testid="emergency-resume-dialog">
        <DialogHeader>
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-feedback-alert-primary/10">
              <Icon name="rotate-ccw" size={20} className="text-feedback-warning-primary" />
            </div>
            <div className="space-y-1">
              <DialogTitle className="text-base">
                {scope === "platform" ? t_i18n("Resume the whole platform?") : t_i18n("Resume your activity?")}
              </DialogTitle>
              <DialogDescription className="content-base leading-relaxed">
                {scope === "platform"
                  ? t_i18n("Lifts the emergency stop for the whole platform: re-enables every assignment it paused and sets AI back to what it was before the stop. Assignments paused by hand are left alone.")
                  : t_i18n("Lifts your emergency stop: re-enables the assignments it paused for you. Assignments paused by hand are left alone.")}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="space-y-2 pt-1">
          <label htmlFor="emergency-resume-reason" className="content-compact text-default-secondary">
            {t_i18n("Why are you lifting the emergency stop? This is recorded in the audit trail.")}
          </label>
          <textarea
            id="emergency-resume-reason"
            autoFocus
            rows={3}
            disabled={busy}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={t_i18n("Incident resolved, root cause identified…")}
            className="w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base text-default-primary placeholder:text-default-secondary focus:outline-hidden focus-visible:ring-2 focus-visible:ring-focus focus:border-accent disabled:opacity-50"
          />
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            size="sm"
            className="bg-feedback-alert-primary hover:bg-feedback-alert-primary/90 text-on-feedback-fill border-0"
            disabled={!canSubmit}
            onClick={submit}
            data-testid="emergency-resume-confirm"
          >
            {busy ? t_i18n("Resuming…") : t_i18n("Resume")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
