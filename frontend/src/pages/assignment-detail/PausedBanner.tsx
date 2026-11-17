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
 * Explains WHY an assignment is paused.
 *
 * A "Paused" badge on its own is a dead end: seven unrelated code paths write
 * `enabled = false` — a human toggling the switch, an agent calling the
 * agent-builder `update_assignment` tool, the integration auth-failure
 * quarantine, the stale-integration-reference pause, the scheduler
 * disabling an unparseable cron, seeding, and the copy paths (duplicate /
 * import / catalog install) that create one switched off — and until the
 * backend started stamping `paused_*` on the assignment, an automation that
 * silently stopped firing was indistinguishable from one somebody paused on
 * purpose weeks ago.
 *
 * Renders nothing when the assignment is enabled, unless its owner's account
 * is deactivated, which pauses it too. When it is paused with no
 * stamp (paused before this was tracked) it says so plainly rather than
 * inventing a cause. A manager gets the resume action right here, next to the
 * reason it was paused for.
 */

import { Bot, CalendarX, HelpCircle, KeyRound, Link2Off, OctagonX, PackagePlus, Play, Settings2, UserRound, Workflow } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { OwnerDeactivatedNotice } from "@/components/ui/owner-deactivated-notice";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn, formatShortDateTime } from "@/lib/utils";
import type { Assignment, AssignmentPauseReason } from "@/types";
import { Spinner } from "@filigran/design-system";

interface PausedBannerProps {
  assignment: Assignment;
  /** Re-enables the assignment. Omitted when the viewer cannot manage it. */
  onResume?: () => void;
  resuming?: boolean;
}

/** Icon + one-line headline per cause. Amber for a deliberate pause, red for
 *  an automatic one — an auto-pause means something is broken upstream. */
const REASON_META: Partial<Record<AssignmentPauseReason, { icon: LucideIcon; headline: string; severity: "info" | "error" }>> = {
  user_toggle: { icon: UserRound, headline: "Paused by a user", severity: "info" },
  agent_tool: { icon: Bot, headline: "Paused by an agent", severity: "info" },
  auth_quarantine: { icon: KeyRound, headline: "Auto-paused: integration authentication keeps failing", severity: "error" },
  stale_reference: { icon: Link2Off, headline: "Auto-paused: an integration resource it uses is missing", severity: "error" },
  invalid_schedule: { icon: CalendarX, headline: "Auto-paused: the schedule is invalid", severity: "error" },
  created_disabled: { icon: PackagePlus, headline: "Created switched off, awaiting review", severity: "info" },
  system_seed: { icon: Settings2, headline: "Switched off by the deployment configuration", severity: "info" },
  // Red on purpose although a human pressed it: an emergency stop is the one
  // pause that must never read as routine.
  panic_stop: { icon: OctagonX, headline: "Stopped by an emergency stop", severity: "error" },
  // A flow took its trigger over (#8260). Resuming it does not deactivate the
  // flow: both then run, and the Concierge's watch leaves that choice alone.
  concierge_switch_over: { icon: Workflow, headline: "Replaced by a flow after an approved Concierge proposal", severity: "info" },
};

export function PausedBanner({ assignment, onResume, resuming = false }: PausedBannerProps) {
  const { t_i18n } = useFormatter();

  // Shown above the regular explanation, which still applies once the account
  // is back when the owner had paused it.
  const ownerNotice = assignment.paused_by_owner_deactivation ? (
    <OwnerDeactivatedNotice
      entity="assignment"
      ownerId={assignment.user_id}
      description={t_i18n("Nothing starts this assignment while its owner's account is deactivated: schedules, webhooks, integration events, chaining, flows and manual runs are all held. It resumes as it was once the account is reactivated.")}
      reactivateHint={t_i18n("Reactivate the account from the user's page to resume this assignment.")}
    />
  ) : null;
  if (assignment.enabled) return ownerNotice;

  const meta = assignment.paused_reason ? REASON_META[assignment.paused_reason] : undefined;
  const Icon = meta?.icon ?? HelpCircle;
  const isError = meta?.severity === "error";
  const tone = isError
    ? { wrap: "bg-feedback-error-primary/5", icon: "text-feedback-error-primary", title: "text-feedback-error-primary", body: "text-feedback-error-primary/80" }
    : { wrap: "bg-feedback-alert-primary/5", icon: "text-feedback-warning-primary", title: "text-feedback-warning-primary", body: "text-feedback-warning-primary" };

  const headline = meta
    ? t_i18n(meta.headline)
    : t_i18n("Paused: reason unknown");

  // The actor is only worth naming when it is a person; the system labels
  // ("XTM One — scheduler") already read as the subject of the headline.
  const actor = assignment.paused_by_user_id ? assignment.paused_by_label : null;
  const when = assignment.paused_at ? formatShortDateTime(assignment.paused_at) : null;

  const attribution = [
    actor && t_i18n("by {actor}", { actor }),
    when && t_i18n("on {date}", { date: when }),
  ]
    .filter(Boolean)
    .join(" ");

  const pauseBanner = (
    <div className={cn("rounded-sm p-3 flex items-start gap-2.5", tone.wrap)}>
      <Icon className={cn("h-4 w-4 shrink-0 mt-0.5", tone.icon)} />
      <div className="space-y-1 flex-1 min-w-0">
        <p className={cn("content-compact font-medium leading-relaxed", tone.title)}>
          {headline}
          {attribution && <span className="font-normal"> {attribution}</span>}
        </p>
        {assignment.paused_details && (
          <p className={cn("content-compact leading-relaxed", tone.body)}>{assignment.paused_details}</p>
        )}
        {!assignment.paused_reason && (
          <p className={cn("content-compact leading-relaxed", tone.body)}>
            {t_i18n("This assignment was paused before pause tracking was added, so there is no record of who or what paused it. The next pause will be attributed.")}
          </p>
        )}
      </div>
      {onResume && (
        <Tip content={t_i18n("Re-enable this assignment so its triggers fire again.")}>
          <Button variant="outline" size="sm" className="h-7 shrink-0 gap-1.5 content-compact" disabled={resuming} onClick={onResume} aria-busy={resuming || undefined}>
            {resuming && <Spinner size="sm" tone="inherit" />}{!resuming && <Play className="h-3 w-3" />}
            {t_i18n("Resume")}
          </Button>
        </Tip>
      )}
    </div>
  );

  return ownerNotice ? (
    <>
      {ownerNotice}
      {pauseBanner}
    </>
  ) : (
    pauseBanner
  );
}
