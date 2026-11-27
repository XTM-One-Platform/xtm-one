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
 * Download a dashboard as a PDF (every tab, or the tab on screen, each tab a
 * page) or as a PNG (the tab on screen), rendered on the server with the
 * caller's own access (`useExportDashboard`, `dashboard_export.py`) - or schedule
 * it by email (`DashboardScheduleDialog`).
 */
import { useState } from "react";
import {
  CalendarClock,
  Download,
  FileImage,
  FileText,
} from "lucide-react";
import { Spinner } from "@filigran/design-system";

import { OverflowMenu } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import {
  useExportDashboard,
  type DashboardRenderExport,
} from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";
import { saveBlobAs } from "@/lib/utils";
import type { CustomDashboard } from "@/types";

import { DashboardScheduleDialog } from "./DashboardScheduleDialog";

export function DashboardExportMenu({
  dashboardId,
  title,
  tabId,
  tabCount,
  className,
  schedule,
}: {
  dashboardId: string;
  title: string;
  /** The tab on screen. */
  tabId: string | null;
  tabCount: number;
  className?: string;
  /** The dashboard, to offer scheduling its export by email. */
  schedule?: CustomDashboard;
}) {
  const [scheduling, setScheduling] = useState(false);
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const exporting = useExportDashboard(dashboardId);
  const run = (body: DashboardRenderExport, fallback: string) =>
    exporting.mutate(body, {
      onSuccess: ({ blob, filename }) => saveBlobAs(blob, filename ?? fallback),
      onError: () =>
        toast({
          title: t_i18n("The dashboard could not be exported"),
          description: t_i18n("Try again in a moment."),
          variant: "error",
        }),
    });
  const stem = title.trim() || "dashboard";
  const thisTab = tabId ? [tabId] : undefined;
  return (
    <>
      <OverflowMenu
        label={t_i18n("Download")}
        className={className}
        busy={exporting.isPending}
        trigger={
          exporting.isPending ? (
            <Spinner size="sm" tone="inherit" label={t_i18n("Exporting...")} />
          ) : (
            <Download className="h-4 w-4" />
          )
        }
        items={[
          {
            id: "pdf",
            label: tabCount > 1 ? t_i18n("PDF of every tab") : t_i18n("PDF"),
            icon: FileText,
            onSelect: () => run({ format: "pdf" }, `${stem}.pdf`),
          },
          ...(tabCount > 1
            ? [
                {
                  id: "pdf-tab",
                  label: t_i18n("PDF of this tab"),
                  icon: FileText,
                  onSelect: () =>
                    run({ format: "pdf", layout_ids: thisTab }, `${stem}.pdf`),
                },
              ]
            : []),
          {
            id: "png",
            label: tabCount > 1 ? t_i18n("PNG of this tab") : t_i18n("PNG"),
            icon: FileImage,
            onSelect: () =>
              run({ format: "png", layout_ids: thisTab }, `${stem}.png`),
          },
          ...(schedule
            ? [
                {
                  id: "schedule",
                  label: t_i18n("Schedule an export..."),
                  icon: CalendarClock,
                  separatorBefore: true,
                  onSelect: () => setScheduling(true),
                },
              ]
            : []),
        ]}
      />
      {/* Mounted when opened: its agents and assignments are read only then. */}
      {schedule && scheduling && (
        <DashboardScheduleDialog
          dashboard={schedule}
          open
          onOpenChange={setScheduling}
        />
      )}
    </>
  );
}
