// SPDX-License-Identifier: Apache-2.0

import { waitingForMePath, workItemPath } from "@/lib/task-routes";
import { Icon } from "@filigran/design-system";
import { ListTodo, ShieldOff } from "lucide-react";
import { useMemo } from "react";
import { useNavigate } from "react-router-dom";

import { DashboardNotice, NoticeAction } from "@/components/ui/dashboard-notice";
import { EnterpriseUpsellNotice } from "@/components/ui/enterprise-upsell";
import { useUpdateProfile } from "@/hooks/api/useAuthApi";
import { useHealthFindings } from "@/hooks/api/useHealthApi";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useWorkItemsWaitingForMe } from "@/hooks/api/useWorkItemApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { HEALTH_MODULE_LABELS, HEALTH_NOTICE_COVERED_CHECKS, checkTitle } from "@/lib/health-modules";
import { exactCount, formatCount, isCompacted } from "@/lib/number-format";
import { isSharedOauthIntegration } from "@/lib/utils";
import type { Integration } from "@/types";

import { KBHealthWarnings } from "./KBHealthWarnings";
import { ModelConfigWarnings } from "./ModelConfigWarnings";

/**
 * Dashboard alert for the work items that wait for the reader: a decision
 * they owe, or an item of theirs that no agent owns, open past its due date.
 * An agent's own waits (on another item, a pull request, a deployment, a run
 * holding it) are never the reader's to-do and stay in Operations. Names the
 * item waiting longest; Review opens that item when it is the only one, and
 * otherwise the Tasks "Waiting for me" view on the All segment, which lists
 * every item this notice counts (`waitingForMePath`). Renders nothing when nothing
 * waits.
 */
function WorkItemsWaitingForMeAlert() {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const { data } = useWorkItemsWaitingForMe();
  const count = data?.count ?? 0;
  const oldest = data?.oldest ?? null;
  if (count <= 0) return null;
  const since = oldest?.since ? intl.formatDate(oldest.since, { dateStyle: "medium" }) : null;
  const reason = oldest?.reason || oldest?.title || "";
  const open = () =>
    navigate(
      count === 1 && oldest
        ? workItemPath(oldest.id)
        : waitingForMePath(),
    );
  return (
    <DashboardNotice
      variant="warning"
      title={
        // Compact from 1,000 ("1.2K work items ..."), the exact count on hover (#10476).
        <span title={isCompacted(count) ? exactCount(intl.locale, count) : undefined}>
          {t_i18n("{count, plural, one {{shown} work item is waiting for you} other {{shown} work items are waiting for you}}", {
            count,
            shown: formatCount(intl.locale, count),
          })}
        </span>
      }
      onClick={open}
      actions={
        <NoticeAction variant="warning" onClick={open}>
          <ListTodo className="h-4 w-4" aria-hidden /> {t_i18n("Review")}
        </NoticeAction>
      }
    >
      {since
        ? t_i18n("Waiting for you since {date}: {reason}", { date: since, reason })
        : t_i18n("Waiting for you: {reason}", { reason })}
    </DashboardNotice>
  );
}

/**
 * Dashboard alert for the critical findings Platform Health holds open (#101 S8b): the notice
 * reads the engine's findings, never a second implementation of a check. Administrators only (the
 * Health page is theirs). The checks a dedicated notice already says (model configuration,
 * knowledge base health: `HEALTH_NOTICE_COVERED_CHECKS`) are left to that notice. Names the most
 * recent one; Review opens Platform Health on its Overview tab (`/health`: the page reads its tab
 * from the path, never from a query). Renders nothing when none is open.
 */
function HealthCriticalFindingsAlert() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { isAdmin } = useAuth();
  const { data } = useHealthFindings({ severity: ["critical"], status: ["open"], limit: 200 }, { enabled: isAdmin });
  if (!isAdmin) return null;
  const findings = (data?.items ?? []).filter((f) => !HEALTH_NOTICE_COVERED_CHECKS.has(f.check_id));
  if (findings.length === 0) return null;
  const latest = findings.reduce((a, b) => (b.last_seen > a.last_seen ? b : a));
  // Overview is the default tab: the bare entry route (#797).
  const open = () => navigate("/health");
  const where = t_i18n(HEALTH_MODULE_LABELS[latest.module] ?? latest.module);
  const what = latest.subject_name
    ? `${checkTitle(latest.check_id, t_i18n)} (${latest.subject_name})`
    : checkTitle(latest.check_id, t_i18n);
  return (
    <DashboardNotice
      variant="error"
      title={t_i18n("{count, plural, one {# critical finding is open in Platform Health} other {# critical findings are open in Platform Health}}", { count: findings.length })}
      onClick={open}
      actions={
        <NoticeAction variant="error" onClick={open}>
          <Icon name="compass" size={16} aria-hidden /> {t_i18n("Review")}
        </NoticeAction>
      }
    >
      {t_i18n("Most recent, {module}: {finding}", { module: where, finding: what })}
    </DashboardNotice>
  );
}

/** The current user's own OAuth integrations sharing their connected account. */
function filterSharedOauth(
  integrations: Integration[] | undefined,
  userId: string | undefined,
): Integration[] {
  return (integrations ?? []).filter(
    (i) => i.user_id === userId && isSharedOauthIntegration(i) && i.oauth_share_acknowledged,
  );
}

/**
 * Dashboard alert for OAuth integrations sharing their owner's connected
 * account (company-wide, with a group or with a person) — what sharing an
 * OAuth integration does unless its owner opts out. A standing
 * reminder that other members can act using that personal token until the
 * owner turns it off, not just a one-time warning at share time.
 * Dismissible without unsharing: the X keeps sharing on. The
 * dismissal is stored on the user (`User.oauth_share_dismissed_integration_ids`)
 * rather than in localStorage, so closing it once closes it on every
 * browser and machine; the server keeps the list honest (only the owner's
 * own, currently shared OAuth integrations) and drops an id the moment that
 * integration's sharing is turned off, so re-sharing it — or sharing a new
 * one — brings the notice back. Renders nothing when the current user shares
 * none of their own (or has dismissed every currently shared id).
 */
function SharedOauthCredentialsAlert() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { user } = useAuth();
  const { data: integrations } = useIntegrations();
  const updateProfile = useUpdateProfile();
  const userId = user?.id;
  const dismissedIds = user?.oauth_share_dismissed_integration_ids;
  const dismissed = useMemo(() => new Set(dismissedIds ?? []), [dismissedIds]);

  const shared = filterSharedOauth(integrations, userId);
  const visible = shared.filter((i) => !dismissed.has(i.id));
  if (visible.length === 0) return null;
  const single = visible.length === 1 ? visible[0] : null;
  const goToSharing = () => navigate(single ? `/integrations/${single.id}` : "/integrations");
  const keepSharing = t_i18n("Keep sharing");
  return (
    <DashboardNotice
      variant="warning"
      title={t_i18n("{count, plural, one {# of your integrations shares your personal OAuth credentials} other {# of your integrations share your personal OAuth credentials}}", { count: visible.length })}
      onClick={goToSharing}
      onDismiss={() => {
        // Whole-field write, like every preference: the ids already closed
        // plus every one on screen. `useUpdateProfile` patches `["me"]`
        // optimistically, so the notice disappears at once and comes back
        // by itself if the round-trip fails.
        const next = new Set(dismissed);
        for (const i of visible) next.add(i.id);
        updateProfile.mutate({ oauth_share_dismissed_integration_ids: [...next] });
      }}
      dismissLabel={keepSharing}
      actions={
        <NoticeAction variant="warning" onClick={goToSharing}>
          <ShieldOff className="h-4 w-4" aria-hidden /> {t_i18n("Stop sharing")}
        </NoticeAction>
      }
    >
      {t_i18n("Other members can act using your personal token until you turn this off.")}
    </DashboardNotice>
  );
}

/**
 * The single, unified dashboard notices region: every system/health/work alert
 * rendered with one layout, one size and a consistent semantic color, in one
 * place (the top of the dashboard). Mount this once, above the hero, on every
 * dashboard so the experience is identical everywhere. Renders nothing when
 * there is nothing to surface.
 *
 * On Community Edition the upsell notice leads, and the model / provider
 * configuration warnings below it stay silent: they would walk the person
 * through configuring an LLM that the edition cannot use, which is a setup
 * that changes nothing. They return the moment Enterprise Edition is active.
 *
 * `empty:hidden`: with no notice to show the wrapper has no box, so the page's
 * `space-y-*` stack gives it no margin and the first row below sits at the
 * page's top padding instead of one stack gap lower (#11199).
 */
export function DashboardNotices() {
  return (
    <div className="space-y-3 empty:hidden">
      <EnterpriseUpsellNotice />
      <ModelConfigWarnings />
      <KBHealthWarnings />
      <HealthCriticalFindingsAlert />
      <WorkItemsWaitingForMeAlert />
      <SharedOauthCredentialsAlert />
    </div>
  );
}
