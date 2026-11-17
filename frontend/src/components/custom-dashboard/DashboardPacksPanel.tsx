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
 * The Dashboard Studio's Packs panel: ready-made widgets to drop into the
 * dashboard on the stage.
 *
 * A pack is picked, its live widgets are bound to an integration of their
 * platform the person can use (asked only when they have several), and
 * "Insert" writes them below the selected widget - or at the end - at the
 * revision read (`POST /dashboard-widget-packs/{id}/insert`, validated like any
 * write). A file can be imported as a new pack from here too.
 */
import { useMemo, useRef, useState } from "react";
import { Spinner } from "@filigran/design-system";
import { Boxes, Plus, Upload } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import {
  useImportWidgetPack,
  useInsertWidgetPack,
  usePackIntegrations,
  useWidgetPacks,
} from "@/hooks/api/useWidgetPackApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { CustomDashboard, PackIntegration, WidgetPack } from "@/types";

import { PLATFORM_NAMES } from "./spec";

function choicesBySource(integrations: PackIntegration[] | undefined) {
  const out: Record<string, PackIntegration[]> = {};
  for (const integration of integrations ?? []) (out[integration.provider] ??= []).push(integration);
  return out;
}

export function DashboardPacksPanel({
  dashboard,
  afterWidgetId,
  onInserted,
}: {
  dashboard: CustomDashboard;
  /** Insert below this widget (the one selected); unset: at the end. */
  afterWidgetId: string | null;
  onInserted: (ids: string[]) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const packs = useWidgetPacks("all");
  const integrations = usePackIntegrations();
  const insert = useInsertWidgetPack(dashboard.id);
  const importPack = useImportWidgetPack();
  const fileInput = useRef<HTMLInputElement>(null);
  const [pickedId, setPickedId] = useState<string | null>(null);
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const bySource = useMemo(() => choicesBySource(integrations.data), [integrations.data]);
  const picked: WidgetPack | undefined = packs.data?.find((p) => p.id === pickedId);

  const missing = picked?.sources.filter((source) => (bySource[source]?.length ?? 0) === 0) ?? [];
  const toChoose = picked?.sources.filter((source) => (bySource[source]?.length ?? 0) > 1) ?? [];
  const ready = !!picked && missing.length === 0 && toChoose.every((source) => !!bindings[source]);

  const doInsert = () => {
    if (!picked) return;
    insert.mutate(
      {
        packId: picked.id,
        revision: dashboard.revision,
        bindings: Object.fromEntries(toChoose.map((source) => [source, bindings[source]])),
        ...(afterWidgetId ? { after: afterWidgetId } : {}),
      },
      {
        onSuccess: (result) => {
          toast({ title: t_i18n("{count} widgets inserted", { count: result.inserted.length }), variant: "success" });
          onInserted(result.inserted);
        },
        onError: (error) =>
          toast({ title: t_i18n("The widgets could not be inserted"), description: error.message, variant: "error" }),
      },
    );
  };

  const onFile = (file: File | undefined) => {
    if (!file) return;
    importPack.mutate(file, {
      onSuccess: (pack) => {
        setPickedId(pack.id);
        toast({
          title: t_i18n("Pack imported"),
          description: pack.notes.length ? pack.notes.slice(0, 3).join(" ") : undefined,
          variant: "success",
        });
      },
      onError: (error) => toast({ title: t_i18n("The file could not be imported"), description: error.message, variant: "error" }),
    });
  };

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-4" data-testid="dashboard.studio.packs">
      <div className="flex items-center justify-between gap-2">
        <span className="content-caption text-default-secondary">{t_i18n("Ready-made widgets")}</span>
        <Button
          variant="outline"
          size="sm"
          className="h-7 gap-1 content-caption"
          disabled={importPack.isPending}
          aria-busy={importPack.isPending || undefined}
          onClick={() => fileInput.current?.click()}
        >
          {importPack.isPending && <Spinner size="sm" tone="inherit" />}
          {!importPack.isPending && <Upload className="h-3 w-3" />}
          {t_i18n("Import")}
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
      {packs.isPending && <Skeleton className="h-40 w-full" />}
      <ul className="flex flex-col gap-1.5">
        {(packs.data ?? []).map((pack) => (
          <li key={pack.id}>
            <button
              type="button"
              onClick={() => setPickedId(pack.id)}
              aria-pressed={pickedId === pack.id}
              className={cn(
                "flex w-full flex-col gap-1 rounded-sm border border-elevation-subtle px-3 py-2 text-left hover:bg-elevation-hover",
                pickedId === pack.id && "border-accent bg-accent/5",
              )}
            >
              <span className="flex items-center gap-2 content-compact font-medium text-default-primary">
                <Boxes className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
                <span className="min-w-0 truncate">{pack.name}</span>
              </span>
              <span className="flex flex-wrap items-center gap-1 content-caption text-default-secondary">
                {t_i18n("{count} widgets", { count: pack.widget_count })}
                {pack.builtin && <Badge variant="outline">{t_i18n("Built-in")}</Badge>}
                {pack.sources.map((source) => (
                  <Badge key={source} variant="cyan">
                    {PLATFORM_NAMES[source]}
                  </Badge>
                ))}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {picked && (
        <div className="flex flex-col gap-2 rounded-sm border border-elevation-subtle p-3">
          {picked.description && <p className="content-caption text-default-secondary">{picked.description}</p>}
          {missing.map((source) => (
            <p key={source} className="content-caption text-feedback-warning-primary">
              {t_i18n("You can use no {platform} integration: its widgets cannot be inserted.", {
                platform: PLATFORM_NAMES[source],
              })}
            </p>
          ))}
          {toChoose.map((source) => (
            <div key={source} className="flex flex-col gap-1">
              <span className="content-caption text-default-secondary">
                {t_i18n("{platform} to read from", { platform: PLATFORM_NAMES[source] })}
              </span>
              <SearchableSelect
                aria-label={t_i18n("{platform} to read from", { platform: PLATFORM_NAMES[source] })}
                options={bySource[source].map((integration) => ({ value: integration.id, label: integration.name }))}
                value={bindings[source] ?? ""}
                onValueChange={(value) => setBindings((b) => ({ ...b, [source]: value }))}
                placeholder={t_i18n("Choose an integration")}
              />
            </div>
          ))}
          {dashboard.can_manage && (
            <Button className="gap-1.5" disabled={!ready || insert.isPending} aria-busy={insert.isPending || undefined} onClick={doInsert}>
              {insert.isPending && <Spinner size="sm" tone="inherit" />}
              {!insert.isPending && <Plus className="h-4 w-4" />}
              {afterWidgetId ? t_i18n("Insert below the selected widget") : t_i18n("Insert into this dashboard")}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
