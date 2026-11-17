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
 * Full-width notice shown on every page while an emergency stop is in effect
 * for the viewer: their assignments are paused with `panic_stop` (their own
 * stop, or a platform-wide one), or — for an administrator — the platform AI
 * override is OFF. Chat, agents, operations: wherever they look, the state of
 * the platform is stated in one line, with who pressed it, when and why.
 */
import { useState } from "react";
import { OctagonX, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useEmergencyStopStatus, type PanicScope } from "@/hooks/api/usePanicApi";
import { useMe } from "@/hooks/api/useAuthApi";
import { useFormatter } from "@/i18n";
import { formatShortDateTime } from "@/lib/utils";
import { EmergencyResumeDialog } from "./EmergencyResumeDialog";

export function EmergencyStopBanner() {
  const { t_i18n } = useFormatter();
  const { data: user } = useMe();
  const { data: status } = useEmergencyStopStatus(!!user);
  const [resumeScope, setResumeScope] = useState<PanicScope | null>(null);
  if (!status?.active) return null;

  // The direct path: one click opens the same resume dialog the profile menu
  // does (the menu entry stays for those who look there). An administrator
  // lifts what was pressed — a platform-wide stop as a whole, otherwise their
  // own activity; everyone else can only lift their own.
  const isAdmin = user?.role === "admin";
  // A platform-wide stop is lifted as a whole, by an administrator: a
  // self-scope resume is refused by the server while it is in effect, so a
  // regular user gets no button, only the fact.
  const ctaScope: PanicScope | null = status.platform_stop_active
    ? isAdmin
      ? "platform"
      : null
    : "self";

  const paused = Math.max(status.self_paused_assignments, status.platform_paused_assignments ?? 0);
  const facts = [
    paused > 0 ? t_i18n("{count} assignments paused", { count: paused }) : null,
    status.ai_disabled ? t_i18n("AI switched off for everyone") : null,
  ].filter(Boolean);
  const last = status.last_stop;
  const who = last?.by
    ? t_i18n("Pressed by {by} on {date}.", { by: last.by, date: last.at ? formatShortDateTime(last.at) : "—" })
    : null;

  return (
    <>
      <div
        role="alert"
        data-testid="emergency-stop-banner"
        className="flex items-start gap-3 border-b border-feedback-error-primary/40 bg-feedback-error-primary/15 px-4 py-2.5 content-base text-feedback-error-primary sm:px-6"
      >
        <OctagonX className="mt-0.5 h-4 w-4 shrink-0 text-feedback-error-primary" />
        <div className="min-w-0 flex-1 leading-relaxed">
          <span className="font-semibold uppercase tracking-wide text-feedback-error-primary">{t_i18n("Emergency stop in effect")}</span>
          {facts.length > 0 && <span> — {facts.join(", ")}.</span>}
          {who && <span className="text-feedback-error-primary/90"> {who}</span>}
          {last?.reason && <span className="text-feedback-error-primary/90"> {t_i18n("Reason: {reason}", { reason: last.reason })}</span>}
          <span className="text-feedback-error-primary/70">
            {" "}
            {ctaScope === null
              ? t_i18n("Only an administrator can lift it.")
              : t_i18n("Also available from your profile menu.")}
          </span>
        </div>
        {ctaScope !== null && (
          <Button
            size="sm"
            className="shrink-0 gap-1.5 bg-feedback-alert-primary hover:bg-feedback-alert-primary/90 text-on-feedback-fill border-0"
            onClick={() => setResumeScope(ctaScope)}
            data-testid="emergency-stop-banner-resume"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            {ctaScope === "platform" ? t_i18n("Resume the whole platform") : t_i18n("Resume my activity")}
          </Button>
        )}
      </div>
      <EmergencyResumeDialog scope={resumeScope} onClose={() => setResumeScope(null)} />
    </>
  );
}
