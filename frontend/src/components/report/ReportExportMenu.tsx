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
 * Export menu for a report — shared by the chat card and the standalone
 * viewer so the two never offer different exports.
 *
 * `PDF` is a SERVER-side render: the platform prints the report in its own
 * headless browser, the way the viewer's print route draws it (A4, every annex
 * open), and hands back the file, so it does not depend on the reader's
 * browser or print dialog. `Print to PDF (browser)` keeps the browser's own
 * print path (`/reports/:id?print`, a page of its own) as the no-server
 * fallback.
 */

import { useState } from "react";
import { Download, FileText, Printer } from "lucide-react";

import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import { useExportReport } from "@/hooks/api/useReportApi";
import { useFormatter } from "@/i18n";
import { withBasePath } from "@/lib/base-path";
import { saveBlobAs } from "@/lib/utils";
import { Spinner } from "@filigran/design-system";

export function ReportExportMenu({
  reportId,
  className,
  compact = false,
}: {
  reportId: string;
  className?: string;
  /** Icon-only trigger (chat card toolbar). */
  compact?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const exportReport = useExportReport();
  const [busy, setBusy] = useState(false);

  const exportPdf = async () => {
    setBusy(true);
    try {
      const { blob, filename } = await exportReport.mutateAsync(reportId);
      saveBlobAs(blob, filename ?? "report.pdf");
    } catch (err) {
      toast({
        title: t_i18n("Export failed"),
        description:
          err instanceof Error && err.message
            ? err.message
            : t_i18n("The platform could not render the report. Try again or use Print to PDF."),
        variant: "error",
      });
    } finally {
      setBusy(false);
    }
  };

  const items: OverflowMenuItem[] = [
    {
      id: "pdf",
      label: t_i18n("PDF (vector)"),
      icon: FileText,
      onSelect: () => void exportPdf(),
    },
    {
      id: "print",
      label: t_i18n("Print to PDF (browser)"),
      icon: Printer,
      // A page of its own: the app chrome around a card would end up in the
      // output, and the print route opens the dialog by itself.
      onSelect: () => {
        window.open(withBasePath(`/reports/${reportId}?print`), "_blank", "noopener,noreferrer");
      },
    },
  ];

  return (
    <OverflowMenu
      items={items}
      label={t_i18n("Export report")}
      className={className}
      busy={busy}
      trigger={
        busy ? (
          <>
            <Spinner size="sm" tone="inherit" />
            {!compact && <span>{t_i18n("Exporting...")}</span>}
          </>
        ) : (
          <>
            <Download className="h-4 w-4" />
            {!compact && <span>{t_i18n("Export")}</span>}
          </>
        )
      }
    />
  );
}
