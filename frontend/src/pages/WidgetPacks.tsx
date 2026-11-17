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
 * Widget packs - `/dashboards/packs`: the library of ready-made dashboard
 * widgets (Dashboard Studio).
 *
 * Built-in packs (read-only, duplicated to be changed), the person's own and
 * those shared with them. A pack is imported from a file - an XTM One pack,
 * an OpenCTI dashboard or widget export, an OpenAEV dashboard export (ZIP) -
 * and what the conversion changed is said; it is exported as an XTM One pack
 * file, duplicated, deleted by its managers. Packs are inserted from a
 * dashboard's studio.
 *
 * On the shared data table (#9378, slice 4): search in names and descriptions,
 * the Mine / Company / Shared / All scope with its counts, Origin and Platform
 * pills (Built-in is an origin), sort by name, widgets, origin, import changes or
 * update, Columns, page size and CSV, the state in the URL (`wp_` keys, the scope
 * in `?vis=` as on every list page); a failed read is the table's error line with
 * Retry, never the page boundary; the card
 * grid is kept behind the grid / list toggle. A client-side table: the API serves
 * at most 100 packs the caller may use.
 */
import { useCallback, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Boxes, Copy, Download, Trash2, Upload } from "lucide-react";
import { Spinner } from "@filigran/design-system";

import { BackButton } from "@/components/ui/back-button";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ConfirmDeleteDialog } from "@/components/ui/confirm-delete-dialog";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { EmptyState } from "@/components/ui/empty-state";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { PLATFORM_NAMES, type PlatformSource } from "@/components/custom-dashboard/spec";
import { useDeleteWidgetPack, useDuplicateWidgetPack, useImportWidgetPack, useWidgetPacks } from "@/hooks/api/useWidgetPackApi";
import { useAuth } from "@/hooks/useAuth";
import { useViewMode } from "@/hooks/useViewMode";
import { useFormatter } from "@/i18n";
import { api } from "@/lib/api";
import { formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";
import { readVisibility, writeVisibility, type Visibility } from "@/lib/visibility";
import { saveBlobAs } from "@/lib/utils";
import type { WidgetPack } from "@/types";

/** The table's keys in the URL: `wp_q`, `wp_sort`, `wp_page`, `wp_size`, `wp_f_origin`, `wp_f_platform`. */
const URL_KEY = "wp_";

/** Where a pack comes from, as the Origin column and pill say it (English, translated). */
const PACK_ORIGIN_LABELS: Record<WidgetPack["origin"], string> = {
  builtin: "Built-in",
  upload: "Uploaded",
  opencti: "OpenCTI",
  openaev: "OpenAEV",
  dashboard: "Dashboard",
};

/**
 * Column widths in px under the fixed layout: the pack column takes what is left and cuts a
 * long name or description with its tooltip. Platform steps aside under 896 px of table width,
 * Import changes under 768 and Updated under 576, so the table never scrolls sideways at 1024.
 */
const PACK_COLUMN_WIDTHS = { widgets: 104, origin: 136, platform: 176, notes: 160, updated: 144, actions: 128 } as const;

/** The scope a pack falls in for the viewer, as `GET /dashboard-widget-packs?scope=` reads it. */
function packScopes(pack: WidgetPack, userId: string | undefined): Visibility[] {
  if (pack.is_company_managed) return ["company"];
  return pack.user_id !== null && pack.user_id === userId ? ["mine"] : ["shared"];
}

function usePackExport(pack: WidgetPack) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  return () =>
    api
      .download(`/dashboard-widget-packs/${pack.id}/export`)
      .then(({ blob }) => saveBlobAs(blob, `${pack.name.replace(/[^\w .-]+/g, "_")}.json`))
      .catch((error: Error) => toast({ title: t_i18n("The pack could not be exported"), description: error.message, variant: "error" }));
}

/** Export, duplicate and (for its managers) delete: the same three buttons on a card and a row. */
function PackActions({ pack, onDelete }: { pack: WidgetPack; onDelete: (pack: WidgetPack) => void }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const duplicate = useDuplicateWidgetPack();
  const exportPack = usePackExport(pack);
  return (
    <span className="inline-flex items-center justify-end gap-1">
      <Tip content={t_i18n("Export")}>
        <Button variant="ghost" size="icon" aria-label={t_i18n("Export the pack")} onClick={() => void exportPack()}>
          <Download className="h-4 w-4" />
        </Button>
      </Tip>
      <Tip content={t_i18n("Duplicate")}>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t_i18n("Duplicate the pack")}
          disabled={duplicate.isPending}
          aria-busy={duplicate.isPending}
          onClick={() =>
            duplicate.mutate(pack.id, {
              onSuccess: () => toast({ title: t_i18n("Pack duplicated"), variant: "success" }),
              onError: (error) => toast({ title: t_i18n("The pack could not be duplicated"), description: error.message, variant: "error" }),
            })
          }
        >
          <Copy className="h-4 w-4" />
        </Button>
      </Tip>
      {pack.can_manage && (
        <Tip content={t_i18n("Delete")}>
          <Button variant="ghost" size="icon" aria-label={t_i18n("Delete the pack")} onClick={() => onDelete(pack)}>
            <Trash2 className="h-4 w-4" />
          </Button>
        </Tip>
      )}
    </span>
  );
}

function PackCard({ pack, onDelete }: { pack: WidgetPack; onDelete: (pack: WidgetPack) => void }) {
  const { t_i18n } = useFormatter();
  return (
    <Card className="flex h-full flex-col gap-3 p-5" data-testid="widget-pack.card">
      <div className="flex items-start gap-2.5">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-accent/10">
          <Boxes className="h-4 w-4 text-accent" aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate content-base font-semibold text-default-primary">{pack.name}</p>
          {pack.description && <p className="line-clamp-2 content-compact text-default-secondary">{pack.description}</p>}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-1.5 content-caption text-default-secondary">
        <span>{t_i18n("{count, plural, one {# widget} other {# widgets}}", { count: pack.widget_count })}</span>
        {pack.builtin && <Badge variant="outline">{t_i18n("Built-in")}</Badge>}
        {!pack.builtin && pack.is_company_managed && <Badge variant="outline">{t_i18n("Company")}</Badge>}
        {pack.sources.map((source) => (
          <Badge key={source} variant="cyan">
            {PLATFORM_NAMES[source]}
          </Badge>
        ))}
      </div>
      {pack.notes.length > 0 && (
        <details className="content-caption text-default-secondary">
          <DisclosureSummary>{t_i18n("What the import changed ({count})", { count: pack.notes.length })}</DisclosureSummary>
          <ul className="mt-1 list-disc pl-4">
            {pack.notes.slice(0, 10).map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        </details>
      )}
      <div className="mt-auto flex items-center justify-end">
        <PackActions pack={pack} onDelete={onDelete} />
      </div>
    </Card>
  );
}

/** The import notes of a row: their count, the first ten in the tooltip. */
function NotesCell({ pack }: { pack: WidgetPack }) {
  const { t_i18n } = useFormatter();
  if (pack.notes.length === 0) return <span className="text-default-secondary">-</span>;
  return (
    <Tip
      content={
        <ul className="list-disc space-y-0.5 pl-4">
          {pack.notes.slice(0, 10).map((note, index) => (
            <li key={index}>{note}</li>
          ))}
        </ul>
      }
    >
      <span
        tabIndex={0}
        className="block truncate rounded-sm text-default-secondary underline decoration-dotted underline-offset-2 outline-none focus-visible:ring-2 focus-visible:ring-focus"
        data-testid="widget-pack.notes"
      >
        {t_i18n("{count, plural, one {# change} other {# changes}}", { count: pack.notes.length })}
      </span>
    </Tip>
  );
}

export default function WidgetPacks() {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { user } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  // The scope lives in `?vis=` (the shared visibility convention), so a reload or a link keeps it.
  const scope = readVisibility(searchParams, undefined, "all");
  const setScope = useCallback(
    (value: Visibility) => setSearchParams((current) => writeVisibility(current, value), { replace: true }),
    [setSearchParams],
  );
  const [viewMode, setViewMode] = useViewMode("widget-packs");
  const { data, isLoading, isError, refetch } = useWidgetPacks("all", true, { tolerant: true });
  const importPack = useImportWidgetPack();
  const remove = useDeleteWidgetPack();
  const [deleting, setDeleting] = useState<WidgetPack | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const allPacks = useMemo(() => data ?? [], [data]);
  const counts = useMemo(() => {
    const result: Record<Visibility, number> = { all: allPacks.length, mine: 0, company: 0, shared: 0 };
    for (const pack of allPacks) for (const key of packScopes(pack, user?.id)) result[key] += 1;
    return result;
  }, [allPacks, user?.id]);
  const rows = useMemo(
    () => (scope === "all" ? allPacks : allPacks.filter((pack) => packScopes(pack, user?.id).includes(scope))),
    [allPacks, scope, user?.id],
  );

  const columns = useMemo<DataTableColumn<WidgetPack>[]>(
    () => [
      {
        key: "name",
        header: "Widget pack",
        cell: (pack) => (
          <div className="min-w-0">
            <TruncatedText text={pack.name} className="font-medium" />
            {pack.description && <TruncatedText text={pack.description} className="content-compact text-default-secondary" />}
          </div>
        ),
        sortValue: (pack) => pack.name.toLowerCase(),
        firstSort: "asc",
        csv: (pack) => pack.name,
      },
      {
        key: "widgets",
        header: "Widgets",
        align: "right",
        width: PACK_COLUMN_WIDTHS.widgets,
        cell: (pack) => pack.widget_count,
        sortValue: (pack) => pack.widget_count,
        csv: (pack) => pack.widget_count,
      },
      {
        key: "origin",
        header: "Origin",
        width: PACK_COLUMN_WIDTHS.origin,
        hideable: true,
        cell: (pack) => (
          <Badge variant="outline" className="max-w-full whitespace-nowrap">
            <span className="truncate">{t_i18n(PACK_ORIGIN_LABELS[pack.origin] ?? pack.origin)}</span>
          </Badge>
        ),
        sortValue: (pack) => t_i18n(PACK_ORIGIN_LABELS[pack.origin] ?? pack.origin),
        csv: (pack) => pack.origin,
      },
      {
        key: "platform",
        header: "Platform",
        width: PACK_COLUMN_WIDTHS.platform,
        hideBelow: "4xl",
        hideable: true,
        cell: (pack) =>
          pack.sources.length === 0 ? (
            <span className="text-default-secondary">-</span>
          ) : (
            <span className="flex min-w-0 items-center gap-1 overflow-hidden">
              {pack.sources.map((source) => (
                <Badge key={source} variant="cyan" className="whitespace-nowrap">
                  {PLATFORM_NAMES[source]}
                </Badge>
              ))}
            </span>
          ),
        csv: (pack) => pack.sources.map((source) => PLATFORM_NAMES[source]).join(", "),
      },
      {
        key: "notes",
        header: "Import changes",
        width: PACK_COLUMN_WIDTHS.notes,
        hideBelow: "3xl",
        hideable: true,
        cell: (pack) => <NotesCell pack={pack} />,
        sortValue: (pack) => pack.notes.length,
        csv: (pack) => pack.notes.join(" | "),
      },
      {
        key: "updated",
        header: "Updated",
        width: PACK_COLUMN_WIDTHS.updated,
        hideBelow: "xl",
        hideable: true,
        className: "whitespace-nowrap text-default-secondary",
        cell: (pack) => {
          const at = pack.updated_at ?? pack.created_at;
          if (!at) return "-";
          return (
            <Tip content={formatShortDateTimeIntl(intl, at)}>
              <span tabIndex={0} className="rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus">
                {formatTimeAgo(intl, at)}
              </span>
            </Tip>
          );
        },
        sortValue: (pack) => pack.updated_at ?? pack.created_at ?? "",
        firstSort: "desc",
        csv: (pack) => pack.updated_at ?? pack.created_at ?? "",
      },
    ],
    [intl, t_i18n],
  );

  const filters = useMemo<DataTableFilter<WidgetPack>[]>(
    () => [
      {
        key: "origin",
        label: "Origin",
        values: (pack) => [pack.origin],
        options: (Object.keys(PACK_ORIGIN_LABELS) as WidgetPack["origin"][]).map((origin) => ({
          value: origin,
          label: t_i18n(PACK_ORIGIN_LABELS[origin]),
        })),
      },
      {
        key: "platform",
        label: "Platform",
        values: (pack) => pack.sources,
        options: (Object.keys(PLATFORM_NAMES) as PlatformSource[]).map((source) => ({ value: source, label: PLATFORM_NAMES[source] })),
      },
    ],
    [t_i18n],
  );

  const onFile = (file: File | undefined) => {
    if (!file) return;
    importPack.mutate(file, {
      onSuccess: (pack) =>
        toast({
          title: t_i18n("Pack imported: {name}", { name: pack.name }),
          description: pack.notes.length
            ? t_i18n("{count} things changed in the conversion: see the pack.", { count: pack.notes.length })
            : undefined,
          variant: "success",
        }),
      onError: (error) => toast({ title: t_i18n("The file could not be imported"), description: error.message, variant: "error" }),
    });
  };

  const nothingYet = !isLoading && !isError && allPacks.length === 0;

  return (
    <div className="space-y-6">
      <BackButton to="/dashboards" label={t_i18n("Dashboards")} className="self-start" />
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold">{t_i18n("Widget packs")}</h1>
          <p className="mt-2 text-default-secondary">
            {t_i18n(
              "Ready-made dashboard widgets: built-in, imported from an OpenCTI or OpenAEV dashboard, or saved from a dashboard. Insert them from a dashboard's studio.",
            )}
          </p>
        </div>
        <Button
          className="gap-1.5"
          disabled={importPack.isPending}
          aria-busy={importPack.isPending}
          onClick={() => fileInput.current?.click()}
        >
          {importPack.isPending && <Spinner size="sm" tone="inherit" />}
          {!importPack.isPending && <Upload className="h-4 w-4" />}
          {t_i18n("Import a pack")}
        </Button>
        <input
          ref={fileInput}
          type="file"
          accept=".json,.zip,application/json,application/zip"
          className="hidden"
          aria-label={t_i18n("Import a widget pack")}
          onChange={(event) => {
            onFile(event.target.files?.[0]);
            event.target.value = "";
          }}
        />
      </div>
      {nothingYet ? (
        <EmptyState
          icon={Boxes}
          title="No widget packs here"
          description="Import an OpenCTI or OpenAEV dashboard export, or save a dashboard as a pack from its studio."
        />
      ) : (
        <DataTable
          rows={rows}
          columns={columns}
          rowKey={(pack) => pack.id}
          defaultSort={{ key: "name", direction: "asc" }}
          searchFields={(pack) => [pack.name, pack.description]}
          searchPlaceholder={t_i18n("Search widget packs...")}
          filters={filters}
          scope={{ value: scope, onChange: setScope, counts }}
          totalAll={allPacks.length}
          view={{ value: viewMode, onChange: setViewMode }}
          gridClassName="grid gap-4 sm:grid-cols-2 lg:grid-cols-3"
          renderGrid={
            viewMode === "grid"
              ? (page) => (
                  <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                    {page.map((pack) => (
                      <PackCard key={pack.id} pack={pack} onDelete={setDeleting} />
                    ))}
                  </div>
                )
              : undefined
          }
          exportName="widget-packs"
          urlKey={URL_KEY}
          storageKey="widget-packs"
          rowLines={2}
          layout="fixed"
          isLoading={isLoading}
          isError={isError}
          errorLabel={t_i18n("The widget packs could not be loaded.")}
          onRetry={() => void refetch()}
          emptyLabel={t_i18n("No widget packs here")}
          noResultsLabel={t_i18n("No widget packs match these filters.")}
          rowActions={(pack) => <PackActions pack={pack} onDelete={setDeleting} />}
          rowActionsWidth={PACK_COLUMN_WIDTHS.actions}
          rowTestId="widget-pack.row"
        />
      )}
      <ConfirmDeleteDialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        name={deleting?.name ?? ""}
        onConfirm={() =>
          deleting &&
          remove.mutate(deleting.id, {
            onSuccess: () => setDeleting(null),
            onError: (error) => toast({ title: t_i18n("The pack could not be deleted"), description: error.message, variant: "error" }),
          })
        }
      />
    </div>
  );
}
