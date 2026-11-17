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
 * Share a custom dashboard - the deck's and the video's Share dialog for a
 * dashboard: per-user and per-group grants with a tier (`can view`: open it;
 * `can edit`: also rearrange, refine and share it), the admin-only
 * company-managed switch, the audience line computed from the DRAFT, and the
 * dashboard's address to copy.
 *
 * Only the sharing fields travel (`PUT .../sharing`), never the spec, so a
 * sharing change never bumps the revision nor races the assistant's edits.
 * A live platform widget is always read with the VIEWER's own access to its
 * integration: sharing the dashboard shares its layout, not anyone's data.
 * With the Dashboard Studio on, its managers may also publish it as a link
 * (`DashboardPublicLinkSection`).
 */
import { EntityShareDialog } from "@/components/ui/entity-share-dialog";
import { useUpdateDashboardSharing } from "@/hooks/api/useCustomDashboardApi";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useFormatter } from "@/i18n";
import { absoluteUrl } from "@/lib/base-path";
import { DASHBOARD_SHARE_PERMISSION_DESCRIPTIONS, DASHBOARD_SHARE_PERMISSION_LABELS } from "@/lib/constants";
import type { CustomDashboard } from "@/types";

import { DashboardPublicLinkSection } from "./DashboardPublicLinkSection";

export function DashboardShareDialog({
  dashboard,
  open,
  onOpenChange,
}: {
  dashboard: CustomDashboard;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const update = useUpdateDashboardSharing(dashboard.id);
  const studio = useFeatureFlag("DASHBOARD_STUDIO");
  return (
    <EntityShareDialog
      entity={dashboard}
      open={open}
      onOpenChange={onOpenChange}
      title={t_i18n("Share dashboard")}
      description={t_i18n(
        "Everyone with access can open the dashboard; editors can also rearrange, refine and share it. Live OpenCTI, OpenAEV and OpenCRQ widgets are read with each viewer's own access: sharing the dashboard never shares your data.",
      )}
      entityType="dashboard"
      permissionLabels={DASHBOARD_SHARE_PERMISSION_LABELS}
      permissionDescriptions={DASHBOARD_SHARE_PERMISSION_DESCRIPTIONS}
      companyLabel={t_i18n("Company dashboard")}
      link={absoluteUrl(`/dashboards/${dashboard.id}`)}
      save={(payload) => update.mutateAsync(payload)}
      saving={update.isPending}
      testId="dashboard.share.dialog"
    >
      {studio && dashboard.can_manage && <DashboardPublicLinkSection dashboardId={dashboard.id} open={open} />}
    </EntityShareDialog>
  );
}
