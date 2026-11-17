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
 * The ROI report's Export CSV / Export PDF pair (#12077 L6a, UX round 1 B2): one
 * component for the Value page, the fleet ROI view and Analytics > Outcomes, so the
 * three pages draw the same buttons, glyphs, busy state and error toast. The query
 * is the scope the page shows (`scope`, or one fleet's with `fleetId`).
 */
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useExportRoiReport, type RoiWindow } from "@/hooks/api/useRoiReportApi";
import { useFormatter } from "@/i18n";

interface RoiExportButtonsProps {
  window: RoiWindow;
  /** The scope of `GET /roi/export`; left out for the platform default or a fleet's. */
  scope?: "platform" | "mine";
  /** One fleet's export. */
  fleetId?: string;
  /** Prefix of the buttons' `data-testid` (`<prefix>-csv`, `<prefix>-pdf`). */
  testId?: string;
}

export function RoiExportButtons({ window, scope, fleetId, testId }: RoiExportButtonsProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const exportReport = useExportRoiReport();
  const busy = exportReport.isPending;
  const onExport = (format: "csv" | "pdf") =>
    exportReport.mutate(
      { window, format, scope, fleetId },
      { onError: (error) => toast({ title: t_i18n("The export failed."), description: error.message, variant: "error" }) },
    );
  return (
    <div className="flex items-center gap-2" aria-busy={busy}>
      <Button type="button" variant="outline" size="sm" className="gap-2" onClick={() => onExport("csv")} disabled={busy} data-testid={testId ? `${testId}-csv` : undefined}>
        <Icon name="download" size={16} aria-hidden />
        {t_i18n("Export CSV")}
      </Button>
      <Button type="button" variant="outline" size="sm" className="gap-2" onClick={() => onExport("pdf")} disabled={busy} data-testid={testId ? `${testId}-pdf` : undefined}>
        <Icon name="file-text" size={16} aria-hidden />
        {t_i18n("Export PDF")}
      </Button>
    </div>
  );
}
