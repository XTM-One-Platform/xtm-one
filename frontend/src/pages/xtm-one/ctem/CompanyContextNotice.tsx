// SPDX-License-Identifier: Apache-2.0

/**
 * The organization context behind a CTEM page (#11464). The only source of
 * company context is the organization profile of Settings (never a knowledge
 * base). The state is the posture's `context` (`GET /ctem-metrics/posture`);
 * nothing is drawn while it is not read yet, so no notice flashes on load.
 *
 * - missing (no profile saved) or partial (a field the CTEM scores read is
 *   empty, the fields named): an info notice;
 * - complete: one quiet confirmation line (`ContextScoredLine`), drawn by the
 *   page header itself, never on a row of its own: the home dashboard toolbar
 *   next to the context bar (#12258), a stage page on its breadcrumb line and
 *   the exposure briefing on its title line (`inline`), so both placements
 *   draw nothing then.
 *
 * Two placements (#11586):
 *
 * - `dashboard` (the home command center): for administrators only (the
 *   people who can complete it), dismissible, the close stored per user
 *   server-side with the fields missing then; it comes back only when a field
 *   outside them becomes missing. No permanent block on the home dashboard.
 * - `brief` (a stage page, the exposure briefing): for every reader, never
 *   dismissible, since it explains the quality of the analysis; the link to
 *   Settings for administrators only.
 */
import { Icon } from "@filigran/design-system";

import { DashboardNotice, NoticeAction } from "@/components/ui/dashboard-notice";
import { ORGANIZATION_SETUP_PATH } from "@/components/layout/OrganizationSetupPrompt";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { FIELD_LABELS } from "@/lib/organization-profile";
import type { CtemContextState } from "@/types";

import { contextNoticeVisible, useNoticeDismissals } from "./notice-dismissal";

/** The words of a missing field, from the Settings form's own labels. */
function contextFieldLabel(path: string): string {
  return (FIELD_LABELS as Record<string, string>)[path] ?? path;
}

export type CompanyContextPlacement = "dashboard" | "brief";

/**
 * The quiet confirmation of a complete context. Default: one 32 px row line
 * (the dashboard toolbar, #12258). `inline`: caption text for a header line,
 * no fixed height so the line keeps its own, last to shrink with an ellipsis
 * and the whole sentence in its title when the line is short of room.
 */
export function ContextScoredLine({ className, inline = false }: { className?: string; inline?: boolean }) {
  const { t_i18n } = useFormatter();
  const sentence = t_i18n("Scored against your organization context.");
  if (inline) {
    return (
      <span
        className={cn("inline-flex min-w-0 shrink items-center gap-1.5 content-caption text-default-secondary", className)}
        data-testid="ctem-context-complete"
        title={sentence}
      >
        <Icon name="check" size={16} className="shrink-0 text-feedback-success-primary" aria-hidden />
        <span className="truncate">{sentence}</span>
      </span>
    );
  }
  return (
    <span
      className={cn("inline-flex h-8 items-center gap-1.5 content-compact text-default-secondary", className)}
      data-testid="ctem-context-complete"
    >
      <Icon name="check" size={16} className="text-feedback-success-primary" />
      {sentence}
    </span>
  );
}

export function CompanyContextNotice({
  context,
  placement,
  className,
}: {
  context: CtemContextState | undefined;
  placement: CompanyContextPlacement;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const { dismissed, dismiss } = useNoticeDismissals();
  if (!context) return null;

  const onDashboard = placement === "dashboard";
  // A complete context is confirmed by the page header itself (the dashboard
  // toolbar, a stage page's breadcrumb line, the briefing's title line), never
  // on a row of its own.
  if (context.state === "complete") return null;

  if (onDashboard && (!isAdmin || !contextNoticeVisible(context.missing_fields, dismissed))) return null;

  const missing = context.state === "missing";
  const fields = context.missing_fields.map((f) => t_i18n(contextFieldLabel(f))).join(", ");
  const testId = missing ? "ctem-context-missing" : "ctem-context-partial";
  const action = isAdmin ? (
    <NoticeAction variant="info" to={ORGANIZATION_SETUP_PATH}>
      {missing ? t_i18n("Set up organization context") : t_i18n("Complete organization context")}
    </NoticeAction>
  ) : undefined;

  if (!onDashboard) {
    return (
      <DashboardNotice
        variant="info"
        className={className}
        title={missing ? t_i18n("No organization context") : t_i18n("Organization context incomplete")}
        actions={action}
      >
        <span data-testid={testId} data-placement="brief">
          {missing
            ? t_i18n("This analysis is not tailored to your organization. It would be sharper with your organization context.")
            : t_i18n("Missing: {fields}. The analysis would be sharper with them.", { fields })}
        </span>
      </DashboardNotice>
    );
  }

  return (
    <DashboardNotice
      variant="info"
      className={className}
      title={missing ? t_i18n("Set up your organization context") : t_i18n("Your organization context is incomplete")}
      actions={action}
      onDismiss={() => dismiss(context.missing_fields)}
      dismissLabel={t_i18n("Dismiss until a new field is missing")}
    >
      <span data-testid={testId} data-placement="dashboard">
        {missing
          ? t_i18n("This analysis and its qualitative score need your organization context: crown jewels, software, sector and risk priorities, set in Settings.")
          : t_i18n("Missing: {fields}. Fill them in Settings to sharpen the analysis and the qualitative score.", { fields })}
      </span>
    </DashboardNotice>
  );
}
