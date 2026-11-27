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
 * A data visualization's menu: Inspect (its data source and the result it
 * drew), Export CSV (that result as a file) and Download PNG (the
 * visualization rendered on the server, `useExportDashboard`);
 * for a platform's widget, Open in its platform (as the modules keep their
 * links in the card's header, not under the chart).
 *
 * Both read the result the widget drew from the query cache: nothing is read
 * again, and nothing but the closed result leaves the page. Inspect shows the
 * data source as stored in the definition (an integration's id, never its
 * credentials) and the answer's status, shape and time. The export is hidden
 * when the definition's `hideExport` is set.
 */
import { useState } from "react";
import { Download, ExternalLink, FileImage, ScanSearch } from "lucide-react";

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { useToast } from "@/components/ui/toast";
import { useDashboardAccess } from "@/hooks/api/dashboard-access";
import { useExportDashboard } from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";
import { safeExternalUrl, saveBlobAs } from "@/lib/utils";

import { csvFileName, toCsv } from "./csv";
import { useDrawnResult } from "./drawn-result";
import { useDashboardLive } from "./live";
import { PLATFORM_NAMES, type DashboardWidget } from "./spec";

type DataWidget = Extract<DashboardWidget, { kind: "platform" | "data" }>;

function Json({ value }: { value: unknown }) {
  return (
    <pre className="max-h-72 overflow-auto rounded-sm border border-elevation-subtle bg-elevation-default p-3 font-mono-plex content-caption leading-5 whitespace-pre-wrap break-words">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function WidgetDataMenu({ widget }: { widget: DataWidget }) {
  const { t_i18n } = useFormatter();
  const live = useDashboardLive();
  const [inspecting, setInspecting] = useState(false);
  // The widget's own query, watched and never run from here: what it drew.
  const result = useDrawnResult(widget);
  const access = useDashboardAccess();
  const { toast } = useToast();
  const exporting = useExportDashboard(live?.dashboardId ?? "");
  // Only a person in the app inspects or exports: never an export's page, never a published snapshot.
  if (!live || access.kind !== "session") return null;
  const sourceId = widget.sourceId;
  const source = sourceId ? live.sources?.[sourceId] : undefined;
  const name = widget.title || widget.id;

  const items: OverflowMenuItem[] = [];
  const openUrl = widget.kind === "platform" && !live.hideOpenLinks ? safeExternalUrl(result?.open_url) : undefined;
  if (openUrl && widget.kind === "platform") {
    const platform = PLATFORM_NAMES[widget.source];
    items.push({
      id: "open",
      label: t_i18n("Open in {platform}", { platform }),
      icon: ExternalLink,
      onSelect: () => window.open(openUrl, "_blank", "noopener,noreferrer"),
    });
  }
  items.push({ id: "inspect", label: t_i18n("Inspect"), icon: ScanSearch, onSelect: () => setInspecting(true) });
  if (!live.hideExport) {
    items.push({
      id: "csv",
      label: t_i18n("Export CSV"),
      icon: Download,
      disabled: result?.status !== "ok",
      onSelect: () => {
        if (result?.status === "ok") saveBlobAs(new Blob([toCsv(result)], { type: "text/csv;charset=utf-8" }), csvFileName(name));
      },
    });
    items.push({
      id: "png",
      label: t_i18n("Download PNG"),
      icon: FileImage,
      disabled: exporting.isPending,
      onSelect: () =>
        exporting.mutate(
          { format: "png", visualization_id: widget.id },
          {
            onSuccess: ({ blob, filename }) => saveBlobAs(blob, filename ?? `${name}.png`),
            onError: () => toast({ title: t_i18n("The visualization could not be exported"), variant: "error" }),
          },
        ),
    });
  }

  return (
    <>
      <OverflowMenu items={items} label={t_i18n("Data of {name}", { name })} className="h-6 w-6" />
      <Dialog open={inspecting} onOpenChange={setInspecting}>
        <DialogContent className="flex max-h-[90dvh] max-w-3xl flex-col gap-4 overflow-y-auto" data-testid="dashboard.inspect">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <ScanSearch className="h-4 w-4 text-accent" aria-hidden="true" />
              {t_i18n("Inspect {name}", { name })}
            </DialogTitle>
            <DialogDescription>
              {t_i18n("The data source this visualization draws, and the result it drew, as you may read it.")}
            </DialogDescription>
          </DialogHeader>
          <section className="flex flex-col gap-2" aria-label={t_i18n("Data source")}>
            <h3 className="content-compact font-semibold text-default-primary">
              {t_i18n("Data source")} {sourceId && <code className="font-mono-plex text-default-secondary">{sourceId}</code>}
            </h3>
            <Json value={source ?? null} />
          </section>
          <section className="flex flex-col gap-2" aria-label={t_i18n("Result")}>
            <h3 className="content-compact font-semibold text-default-primary">{t_i18n("Result")}</h3>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 content-compact">
              <dt className="text-default-secondary">{t_i18n("Status")}</dt>
              <dd>{result ? (result.status === "ok" ? t_i18n("Read") : result.code) : t_i18n("Not read yet")}</dd>
              {result?.status === "ok" && (
                <>
                  <dt className="text-default-secondary">{t_i18n("Shape")}</dt>
                  <dd>{result.shape}</dd>
                  <dt className="text-default-secondary">{t_i18n("Read at")}</dt>
                  <dd>{new Date(result.fetched_at).toLocaleString()}</dd>
                </>
              )}
            </dl>
            {result?.status === "ok" && <Json value={result.data} />}
          </section>
        </DialogContent>
      </Dialog>
    </>
  );
}
