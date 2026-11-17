// SPDX-License-Identifier: Apache-2.0

/**
 * The command center's honest-scope notice (#11213, #11324): a widget whose
 * content comes from module-wide figures or from data that ignores the
 * organization context says so, with the way to set that context up. An
 * administrator gets the call to action ("Set up", or "Review" once a profile
 * exists); anyone else is told who can. Shared by Act now, the exposure
 * briefing and Stats and coverage, so every generic widget reads the same.
 *
 * Every one of them sits on the home command center, where no block is
 * permanent (#11586): the X closes it for this person (stored on the user as
 * `generic:<testId>`, every device); it stays closed while the widget is
 * generic, and Settings > Organization keeps the way to set the context up.
 */
import { DashboardNotice, NoticeAction } from "@/components/ui/dashboard-notice";
import { ORGANIZATION_SETUP_PATH } from "@/components/layout/OrganizationSetupPrompt";
import { useAuth } from "@/hooks/useAuth";
import { useOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";

import { GENERIC_NOTICE_PREFIX, useNoticeDismissals } from "./notice-dismissal";

export interface GenericContextNoticeProps {
  /** The notice title, translated ("These actions are generic"). */
  title: string;
  /** The body an administrator reads, translated: what is generic and what setting it up gives. */
  adminText: string;
  /** The body anyone else reads, translated: the same, and that an administrator can set it up. */
  readerText: string;
  testId: string;
}

export function GenericContextNotice({ title, adminText, readerText, testId }: GenericContextNoticeProps) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const context = useOrganizationContext();
  const { dismissed, dismiss } = useNoticeDismissals();
  const key = `${GENERIC_NOTICE_PREFIX}${testId}`;
  if (dismissed?.includes(key)) return null;
  const hasProfile = context.data?.version != null;
  return (
    <DashboardNotice
      variant="info"
      title={title}
      onDismiss={() => dismiss([key])}
      actions={
        isAdmin ? (
          <NoticeAction variant="info" to={ORGANIZATION_SETUP_PATH}>
            {hasProfile ? t_i18n("Review organization context") : t_i18n("Set up organization context")}
          </NoticeAction>
        ) : undefined
      }
    >
      <span data-testid={testId}>{isAdmin ? adminText : readerText}</span>
    </DashboardNotice>
  );
}
