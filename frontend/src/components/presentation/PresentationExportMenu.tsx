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
 * Export menu for a deck — shared by the chat card and the standalone
 * viewer so the two never offer different formats.
 *
 * Every entry is a SERVER-side render: the platform opens the deck in its own
 * headless browser on a fixed 1920x1080 canvas and hands back the file, so a
 * PDF and a PPTX are pixel-identical to the slides on screen and do not depend
 * on the user's browser, zoom level or print dialog. The browser-print path
 * (`?print-pdf`) stays available from the viewer as a no-server fallback.
 *
 * The last entry is not a render: the JSON package is the deck itself — spec,
 * template reference and every referenced file embedded — the format
 * "Import" on the library page reads, so a deck moves between installs (or
 * out to a backup) without going through the agent again.
 */

import { useState } from "react";
import {
  Download,
  FileJson,
  FileText,
  Presentation as PresentationIcon,
} from "lucide-react";

import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import {
  useExportPresentation,
  useExportPresentationPackage,
} from "@/hooks/api/usePresentationApi";
import { useFormatter } from "@/i18n";
import { saveBlobAs } from "@/lib/utils";
import type { PresentationExportFormat } from "@/types";

import { exportNotes } from "./export-notes";
import { Spinner } from "@filigran/design-system";

type ExportChoice = PresentationExportFormat | "package";

export function PresentationExportMenu({
  presentationId,
  className,
  compact = false,
}: {
  presentationId: string;
  className?: string;
  /** Icon-only trigger (chat card toolbar). */
  compact?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const exportDeck = useExportPresentation();
  const exportPackage = useExportPresentationPackage();
  const [busyFormat, setBusyFormat] = useState<ExportChoice | null>(null);

  const run = async (format: ExportChoice) => {
    setBusyFormat(format);
    try {
      if (format === "package") {
        const { blob, filename } = await exportPackage.mutateAsync(presentationId);
        saveBlobAs(blob, filename ?? "presentation.presentation.json");
      } else {
        const { blob, filename, headers } = await exportDeck.mutateAsync({
          id: presentationId,
          format,
        });
        saveBlobAs(blob, filename ?? `presentation.${format === "pdf" ? "pdf" : "pptx"}`);
        const notes = exportNotes(headers);
        if (notes.length > 0) {
          // What the file could not carry as the viewer shows it: a
          // gauge, map or combo chart exported as a picture. The file is saved
          // either way; the person is told what to expect when they open it.
          toast({
            title: t_i18n("Exported with notes"),
            description: notes.join(" "),
            variant: "info",
          });
        }
      }
    } catch (err) {
      toast({
        title: t_i18n("Export failed"),
        description:
          err instanceof Error && err.message
            ? err.message
            : format === "package"
              ? t_i18n("The platform could not package the deck. Try again.")
              : t_i18n("The platform could not render the deck. Try again or use Print to PDF."),
        variant: "error",
      });
    } finally {
      setBusyFormat(null);
    }
  };

  const items: OverflowMenuItem[] = [
    {
      id: "pdf",
      label: t_i18n("PDF (vector)"),
      icon: FileText,
      onSelect: () => void run("pdf"),
    },
    {
      id: "pptx",
      label: t_i18n("PowerPoint (pixel-perfect)"),
      icon: PresentationIcon,
      onSelect: () => void run("pptx"),
    },
    {
      id: "pptx_native",
      label: t_i18n("PowerPoint (editable shapes)"),
      icon: PresentationIcon,
      onSelect: () => void run("pptx_native"),
    },
    {
      id: "package",
      label: t_i18n("JSON package (import elsewhere)"),
      icon: FileJson,
      onSelect: () => void run("package"),
    },
  ];

  const busy = busyFormat !== null;
  return (
    <OverflowMenu
      items={items}
      label={t_i18n("Export presentation")}
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
