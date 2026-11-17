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
 * A dashboard imported from a file, from the list (`/dashboards`): an XTM One pack, an OpenCTI dashboard,
 * custom view or widget export, an OpenAEV dashboard export (ZIP). Its platform widgets read through the
 * person's integration of their platform; where they have several, they may pick the one here (unused when the
 * file holds no widget of that platform: the server says which to pick when the file needs it). Then the
 * studio opens on it.
 */
import { useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Spinner } from "@filigran/design-system";
import { Upload } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { useImportCustomDashboard } from "@/hooks/api/useCustomDashboardApi";
import { usePackIntegrations } from "@/hooks/api/useWidgetPackApi";
import { useFormatter } from "@/i18n";
import type { PackIntegration } from "@/types";

import { PLATFORM_NAMES, type PlatformSource } from "./spec";

export function ImportDashboardDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  // Closed, it forgets the file and the choices: the next import starts afresh.
  return open ? <ImportDialogBody onOpenChange={onOpenChange} /> : <Dialog open={false} onOpenChange={onOpenChange} />;
}

function ImportDialogBody({ onOpenChange }: { onOpenChange: (open: boolean) => void }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const importing = useImportCustomDashboard();
  const integrations = usePackIntegrations(true);
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [bindings, setBindings] = useState<Record<string, string>>({});
  // The platforms the person reaches through several integrations: those are a choice.
  const toChoose = useMemo(() => {
    const bySource = new Map<string, PackIntegration[]>();
    for (const integration of integrations.data ?? []) bySource.set(integration.provider, [...(bySource.get(integration.provider) ?? []), integration]);
    return [...bySource.entries()].filter(([source, list]) => list.length > 1 && source in PLATFORM_NAMES);
  }, [integrations.data]);
  // The integrations read first: a file sent before would be refused for want of a choice it could have made.
  const ready = !!file && integrations.isSuccess && !importing.isPending;

  const submit = () => {
    if (!file || !ready) return;
    importing.mutate(
      { file, bindings },
      {
        onSuccess: (dashboard) => {
          if (dashboard.import_notes.length) {
            toast({
              title: t_i18n("{count, plural, one {Imported, with # change} other {Imported, with # changes}}", { count: dashboard.import_notes.length }),
              description: dashboard.import_notes.slice(0, 3).join(" "),
              variant: "info",
            });
          }
          onOpenChange(false);
          navigate(`/dashboards/${dashboard.id}/studio`);
        },
        onError: (error) => toast({ title: t_i18n("The file could not be imported"), description: error.message, variant: "error" }),
      },
    );
  };

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="flex max-w-lg flex-col gap-4" data-testid="dashboards.import.dialog">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Upload className="h-4 w-4 text-accent" aria-hidden="true" />
            {t_i18n("Import a dashboard")}
          </DialogTitle>
          <DialogDescription>{t_i18n("An XTM One pack, an OpenCTI dashboard, custom view or widget export, or an OpenAEV dashboard export.")}</DialogDescription>
        </DialogHeader>
        <input
          ref={input}
          type="file"
          accept=".json,.zip,application/json,application/zip"
          className="hidden"
          aria-label={t_i18n("Import a file")}
          data-testid="dashboards.import.file"
          onChange={(event) => {
            setFile(event.target.files?.[0] ?? null);
            event.target.value = "";
          }}
        />
        <div className="flex items-center gap-3">
          <Button variant="outline" onClick={() => input.current?.click()}>
            {t_i18n("Choose a file")}
          </Button>
          <span className="min-w-0 truncate content-compact text-default-secondary">{file ? file.name : t_i18n("No file chosen")}</span>
        </div>
        {toChoose.map(([source, list]) => {
          const label = t_i18n("{platform} to read from", { platform: PLATFORM_NAMES[source as PlatformSource] });
          return (
            <div key={source} className="flex flex-col gap-1">
              <span className="content-caption text-default-secondary">{label}</span>
              <SearchableSelect
                aria-label={label}
                options={list.map((integration) => ({ value: integration.id, label: integration.name }))}
                value={bindings[source] ?? ""}
                onValueChange={(value) => setBindings((current) => ({ ...current, [source]: value }))}
                placeholder={t_i18n("Choose an integration")}
              />
            </div>
          );
        })}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t_i18n("Cancel")}
          </Button>
          <Button className="gap-1.5" disabled={!ready} aria-busy={importing.isPending || undefined} onClick={submit}>
            {importing.isPending && <Spinner size="sm" tone="inherit" />}
            {t_i18n("Import")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
