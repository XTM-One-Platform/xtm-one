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
 * Settings > Access and security > Emergency stop — the emergency stop API token.
 *
 * One platform-wide credential that does exactly one thing: press the
 * emergency stop from outside a session (`POST /api/v1/panic/emergency`), for
 * the runbook on the wall or a monitoring hook. Generated here, shown once,
 * stored as a bcrypt hash. Because it can never be shown again, the section
 * offers to print the emergency procedure — token, command, what happens,
 * how to resume — right after generation, for paper or PDF.
 */
import { useState } from "react";
import { Check, Copy, KeyRound, OctagonX, Printer, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import {
  useEmergencyTokenStatus,
  useGenerateEmergencyToken,
  useRevokeEmergencyToken,
  type EmergencyTokenCreated,
} from "@/hooks/api/usePanicApi";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";
import { platformOrigin } from "@/lib/base-path";
import { emergencyStopCurl, printEmergencyProcedure } from "@/lib/emergency-procedure";
import { formatShortDateTime } from "@/lib/utils";
import { Spinner } from "@filigran/design-system";

export function EmergencyStopTokenSection() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const platformName = usePlatformName();
  const [ConfirmEl, confirm] = useConfirm();
  const { data: status } = useEmergencyTokenStatus();
  const generate = useGenerateEmergencyToken();
  const revoke = useRevokeEmergencyToken();
  const [created, setCreated] = useState<EmergencyTokenCreated | null>(null);
  const [copied, setCopied] = useState(false);

  // Origin + URL path prefix: the printed curl must reach this
  // platform even when a reverse proxy mounts it under /xtm-one.
  const origin = platformOrigin();

  const handleGenerate = async () => {
    if (status?.configured) {
      const ok = await confirm({
        title: "Rotate the emergency stop token?",
        description: "The current token stops working immediately. Every printed procedure that carries it becomes invalid and must be reprinted.",
        confirmLabel: "Rotate token",
        variant: "warning",
      });
      if (!ok) return;
    }
    generate.mutate(undefined, { onSuccess: (data) => setCreated(data) });
  };

  const handleRevoke = async () => {
    const ok = await confirm({
      title: "Revoke the emergency stop token?",
      description: "The API path of the emergency stop is disabled until a new token is generated. The button in the profile menu keeps working.",
      confirmLabel: "Revoke token",
      variant: "danger",
    });
    if (!ok) return;
    revoke.mutate(undefined, { onSuccess: () => setCreated(null) });
  };

  const handleCopy = () => {
    if (!created) return;
    void navigator.clipboard.writeText(created.token);
    setCopied(true);
    window.setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
  };

  const handlePrint = () => {
    if (!created) return;
    const opened = printEmergencyProcedure({
      platformName,
      origin,
      token: created.token,
      createdAt: formatShortDateTime(created.created_at),
      createdBy: created.created_by,
      labels: {
        title: t_i18n("Emergency stop procedure"),
        subtitle: t_i18n("Keep this page with the on-call material. It is the only copy of the token."),
        whenTitle: t_i18n("When to use it"),
        whenBody: t_i18n("An agent behaves unexpectedly, costs run away, or an incident requires that no AI activity continues, and nobody with an administrator session is available to press the button in the interface."),
        whatTitle: t_i18n("What it does"),
        whatBody: t_i18n("Immediately cancels every running agent activity for every user (runs, background tasks, chat turns, flow runs, pending runner commands), pauses every assignment and switches AI off for the whole platform. Work already in progress is stopped and cannot be resumed."),
        howTitle: t_i18n("How to press it from any terminal"),
        howBody: t_i18n("Run the command below. Replace the reason with what is happening; it is recorded in the audit trail. A success answer lists what was stopped."),
        tokenTitle: t_i18n("Emergency stop token"),
        tokenBody: t_i18n("This token does one thing only: press the emergency stop. It cannot read or change anything else. Anyone holding this page can stop the platform, so store it like a physical key. It can be rotated or revoked at any time in Settings > Access and security > Emergency stop."),
        afterTitle: t_i18n("After the stop"),
        afterBody: t_i18n("Sign in as an administrator: a red banner on every page states what was stopped, by whom and why. Use its Resume button (or the profile menu) to re-enable the paused assignments and switch AI back on. A reason is required and recorded."),
        contactsTitle: t_i18n("Who to inform"),
        contactsBody: t_i18n("Write the names and numbers of the people to call once the stop has been pressed:"),
        generated: t_i18n("Generated on"),
        keepSafe: t_i18n("If this page is lost or exposed, rotate the token."),
      },
    });
    if (!opened) {
      toast({ title: t_i18n("The browser blocked the print window. Allow pop-ups for this site and try again."), variant: "error" });
    }
  };

  return (
    <Card data-testid="emergency-stop-token-section">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <OctagonX className="h-4 w-4 text-feedback-error-primary" />
          {t_i18n("Emergency stop API token")}
        </CardTitle>
        <CardDescription>
          {t_i18n("Lets the emergency stop be pressed from outside a session (a printed runbook, a monitoring hook) with one command. The token does nothing else. It is shown once and stored hashed; print the procedure when you generate it.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center gap-2">
          <span className="content-compact text-default-secondary">{t_i18n("Status")}</span>
          {status?.configured ? (
            <Badge variant="success" className="content-caption">{t_i18n("Configured")}</Badge>
          ) : (
            <Badge variant="outline" className="content-caption">{t_i18n("Not configured")}</Badge>
          )}
        </div>

        {created && (
          <div className="space-y-3 rounded-sm border border-feedback-success-primary/20 bg-feedback-success-primary/10 p-4" data-testid="emergency-token-reveal">
            <p className="content-compact font-medium text-feedback-success-primary">
              {t_i18n("Emergency stop token generated: copy it or print the procedure now, it won't be shown again.")}
            </p>
            <div className="flex items-center gap-2">
              <code className="flex-1 break-all rounded-sm bg-elevation-hover px-3 py-2 font-mono-plex content-compact">{created.token}</code>
              <Button variant="outline" size="sm" className="shrink-0 gap-1.5" onClick={handleCopy}>
                {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                {copied ? t_i18n("Copied") : t_i18n("Copy")}
              </Button>
              <Button size="sm" className="shrink-0 gap-1.5" onClick={handlePrint} data-testid="emergency-token-print">
                <Printer className="h-3 w-3" />
                {t_i18n("Print the procedure")}
              </Button>
            </div>
            <pre className="overflow-x-auto rounded-sm bg-elevation-hover px-3 py-2 font-mono-plex content-compact leading-relaxed text-default-secondary">
              {emergencyStopCurl(origin, created.token)}
            </pre>
          </div>
        )}

        {status?.configured && !created && (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-sm bg-elevation-default p-3">
            <div>
              <p className="content-compact text-default-secondary">{t_i18n("Current token prefix")}</p>
              <code className="font-mono-plex content-base">{status.prefix}</code>
            </div>
            <div className="text-right content-compact text-default-secondary">
              {status.created_by && status.created_at && (
                <p>{t_i18n("Generated by {by} on {date}", { by: status.created_by, date: formatShortDateTime(status.created_at) })}</p>
              )}
              <p>
                {status.last_used_at
                  ? t_i18n("Last used on {date}", { date: formatShortDateTime(status.last_used_at) })
                  : t_i18n("Never used")}
              </p>
            </div>
          </div>
        )}

        <div className="flex gap-2">
          <Button
            variant={status?.configured ? "outline" : "default"}
            size="sm"
            className="gap-1.5"
            onClick={handleGenerate}
            disabled={generate.isPending}
            data-testid="emergency-token-generate" aria-busy={generate.isPending || undefined}>
            {generate.isPending && <Spinner size="sm" tone="inherit" />}{!generate.isPending && <KeyRound className="h-3 w-3" />}
            {status?.configured ? t_i18n("Rotate token") : t_i18n("Generate token")}
          </Button>
          {status?.configured && (
            <Button
              variant="ghost"
              size="sm"
              className="gap-1.5 text-feedback-error-primary hover:text-feedback-error-primary"
              onClick={handleRevoke}
              disabled={revoke.isPending} aria-busy={revoke.isPending || undefined}>
              {revoke.isPending && <Spinner size="sm" tone="inherit" />}{!revoke.isPending && <Trash2 className="h-3 w-3" />}
              {t_i18n("Revoke")}
            </Button>
          )}
        </div>
        {ConfirmEl}
      </CardContent>
    </Card>
  );
}
