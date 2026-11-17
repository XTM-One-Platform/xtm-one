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
 * The editor of a platform data source: a widget of one of the connected
 * platforms, built the way that platform's own widget editor builds it.
 *
 * Every platform is edited by the same form (`PlatformForm`), drawn from the
 * description its adapter serves (`GET /custom-dashboards/platform-forms`):
 * the platforms, their names and what each editor offers come from the
 * server. The form edits the stored configuration in place: what no control
 * shows is written back as it was. Every list comes from the platform through
 * the person's own integration; the result is validated again by the server
 * on save. Preview draws it once, with the person's access, before it is
 * saved, as the widget will be: in its module's view and look when the
 * platform names them (`presentation`), else by the shape of its data. A stored widget the form cannot show (`formCarries`) is not shown as
 * a form: the editor says so and gives nothing to save.
 */
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { usePlatformForms, usePreviewPlatformWidget } from "@/hooks/api/useCustomDashboardApi";
import { usePackIntegrations } from "@/hooks/api/useWidgetPackApi";
import { useFormatter } from "@/i18n";
import type { WidgetData } from "@/types";

import { accentStyle } from "./charts/colors";
import { presentedOptions, TYPE_TO_VIEW } from "./definition";
import { ERROR_MESSAGES } from "./live-errors";
import { Pick } from "./platform-fields";
import { formCarries, newWidget } from "./platform-form";
import { PlatformForm } from "./PlatformForm";
import { DATA_VIEWS, type DataView } from "./spec";
import { drawnAsChart } from "./viz/route";

const PlatformChart = lazy(() => import("./charts/PlatformChart"));
const StudioViz = lazy(() => import("./charts/StudioViz"));

type Raw = Record<string, unknown>;

export interface PlatformOptions {
  source: string;
  integration_id: string;
  native: Raw;
}

/** A platform source's options as an editable form; `onChange` gets the options to save. */
export function PlatformSourceEditor({
  dashboardId,
  initial,
  onChange,
}: {
  dashboardId: string;
  initial?: Partial<PlatformOptions> | null;
  onChange: (options: PlatformOptions | null) => void;
}) {
  const { t_i18n } = useFormatter();
  const forms = usePlatformForms();
  const integrations = usePackIntegrations();
  const entries = useMemo(() => forms.data?.forms ?? [], [forms.data]);
  const [source, setSource] = useState<string>(initial?.source ?? "");
  const [integrationId, setIntegrationId] = useState(initial?.integration_id ?? "");
  // The widget being edited, per platform: the stored one, else a new one once the form is known.
  const [natives, setNatives] = useState<Record<string, Raw>>({});
  const entry = entries.find((item) => item.source === source);
  const preview = usePreviewPlatformWidget(dashboardId);
  const usable = useMemo(
    () => (integrations.data ?? []).filter((integration) => integration.provider === source),
    [integrations.data, source],
  );

  // A new source starts on the first platform the server describes.
  useEffect(() => {
    if (!source && entries[0]) setSource(entries[0].source);
  }, [source, entries]);

  // Decided once, on the widget opened, when its form is known: the form never stands in for one it would change.
  const [carried, setCarried] = useState<boolean | null>(initial?.source ? null : true);
  useEffect(() => {
    if (carried !== null || !forms.data) return;
    const stored = forms.data.forms.find((item) => item.source === initial?.source);
    setCarried(!!stored && formCarries(stored.form, initial?.native));
  }, [carried, forms.data, initial?.source, initial?.native]);

  // The only integration of the platform is implied.
  useEffect(() => {
    if (!usable.some((integration) => integration.id === integrationId)) setIntegrationId(usable[0]?.id ?? "");
  }, [usable, integrationId]);

  // Made once per platform (a new widget holds random ids): the stored one, else the platform's new widget.
  useEffect(() => {
    if (!entry || natives[source]) return;
    const start = initial?.source === source && initial.native ? initial.native : newWidget(entry.form);
    setNatives((current) => (current[source] ? current : { ...current, [source]: start }));
  }, [entry, natives, source, initial?.source, initial?.native]);
  const native = natives[source] ?? null;
  const setNative = useCallback((next: Raw) => setNatives((current) => ({ ...current, [source]: next })), [source]);
  const options = integrationId && carried && native ? { source, integration_id: integrationId, native } : null;
  const key = JSON.stringify(options);
  useEffect(() => {
    onChange(options);
    // `key` stands for `options`: a new object each render, the same content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const result = preview.data;
  if (carried === false) {
    return (
      <p className="content-compact text-default-secondary" data-testid="dashboard.sources.platform.not-carried">
        {t_i18n("This widget has settings this form does not show. Edit it in the source editor, so that none of them is lost.")}
      </p>
    );
  }
  if (forms.isError) {
    return <p className="content-compact text-feedback-error-primary">{t_i18n("The platform editors could not be loaded.")}</p>;
  }
  if (!entry || carried === null) return <p className="content-compact text-default-secondary">{t_i18n("Loading…")}</p>;
  const name = entry.name;
  return (
    <div className="flex flex-col gap-3" data-testid="dashboard.sources.platform">
      <div className="grid gap-3 sm:grid-cols-2">
        <Pick label={t_i18n("Platform")} value={source} onChange={setSource} options={entries.map((item) => ({ value: item.source, label: item.name }))} />
        <Pick
          label={t_i18n("Integration")}
          value={integrationId}
          onChange={setIntegrationId}
          options={usable.map((integration) => ({ value: integration.id, label: integration.name }))}
        />
      </div>
      {!integrationId || !native ? (
        <p className="content-compact text-default-secondary">{t_i18n("You have no {platform} integration to read through.", { platform: name })}</p>
      ) : (
        <PlatformForm form={entry.form} source={source} integrationId={integrationId} native={native} onChange={setNative} />
      )}
      {options && (
        <div className="flex flex-col gap-2 rounded-sm border border-dashed border-elevation-subtle p-3">
          <Button
            variant="outline"
            size="sm"
            className="w-fit"
            disabled={preview.isPending}
            onClick={() => preview.mutate({ kind: "platform", id: "preview", source, integration_id: integrationId, native })}
          >
            {t_i18n("Preview")}
          </Button>
          {preview.isError && <p className="content-compact text-feedback-error-primary">{preview.error.message}</p>}
          {result?.status === "error" && (
            <p className="content-compact text-default-secondary" data-error-code={result.code}>
              {t_i18n(ERROR_MESSAGES[result.code], { platform: name })}
            </p>
          )}
          {result?.status === "ok" && <PreviewDrawing result={result} />}
        </div>
      )}
    </div>
  );
}

/** A preview drawn as the widget will be: its module's view and look (`presentation`), else a view by its shape. */
function PreviewDrawing({ result }: { result: Extract<WidgetData, { status: "ok" }> }) {
  const named = result.presentation && typeof result.presentation === "object" ? (result.presentation as { view?: unknown }).view : undefined;
  const preset = typeof named === "string" ? TYPE_TO_VIEW[named] : undefined;
  const view = (DATA_VIEWS as readonly string[]).includes(preset ?? "") ? (preset as DataView) : undefined;
  const options = presentedOptions({}, view, result.presentation);
  const byShape: DataView = result.shape === "number" ? "number" : result.shape === "rows" ? "list" : result.shape === "series" ? "line" : "donut";
  return (
    <div className="contents" style={accentStyle(options)} data-testid="dashboard.sources.platform.preview" data-view={view ?? byShape}>
      <Suspense fallback={null}>
        {drawnAsChart(result, view, options) ? (
          <PlatformChart data={result} view={view ?? byShape} height={180} options={options} />
        ) : (
          <StudioViz data={result} view={view === "text" ? undefined : view} height={180} options={options} />
        )}
      </Suspense>
    </div>
  );
}
