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
 * The dashboard's source editor: the whole dashboard as it is stored - an
 * OpenCTI manifest, OpenCTI and OpenAEV widgets in their platform's own shape
 * and what XTM One adds under `x_xtm_one` (`manifest`, `dashboard_native.py`)
 * - or one element of the working definition (the picked widget's
 * visualization or data source, the defaults).
 *
 * The whole manifest is written with `PUT /definition`, an element with a
 * `set` operation; both at the revision read and validated server-side like
 * an agent's write, so a mistake comes back as the field it is about
 * (`visualizations.kev.dataSources.primary names no data source 'x'`) and the
 * text stays in the editor to fix. Text that is not JSON is never sent.
 */
import { useEffect, useMemo, useState } from "react";
import { Spinner } from "@filigran/design-system";
import { Braces } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useToast } from "@/components/ui/toast";
import { useQueuedDashboardEdit, useReplaceDashboardDefinition } from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";
import { CodeEditor } from "@/pages/flow-builder/CodeEditor";
import type { CustomDashboard } from "@/types";

import { parseSource, sourceScopes, sourceText, type SourceScope } from "./definition";

/** Each scope's label. @i18n-keys */
const SCOPE_LABELS = {
  dashboard: "Dashboard",
  visualization: "Visualization",
  dataSource: "Data source",
  defaults: "Defaults",
  input: "Input",
} as const satisfies Record<SourceScope["kind"], string>;

export function DashboardSourceDialog({
  dashboard,
  selectedId,
  sourceId = null,
  open,
  onOpenChange,
}: {
  dashboard: CustomDashboard;
  /** The widget picked on the stage: its visualization and data source can be edited alone. */
  selectedId: string | null;
  /** A data source to open the editor on (the Data sources panel), or `inputs.<id>` (the Inputs panel). */
  sourceId?: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const replace = useReplaceDashboardDefinition(dashboard.id);
  const update = useQueuedDashboardEdit(dashboard.id, dashboard.revision);
  const scopes = useMemo(() => sourceScopes(dashboard.spec, selectedId, sourceId), [dashboard.spec, selectedId, sourceId]);
  const [scopeKey, setScopeKey] = useState("dashboard");
  // Opened on a data source, the editor starts there.
  useEffect(() => {
    if (open) setScopeKey(sourceId ? (sourceId.startsWith("inputs.") ? "input" : "dataSource") : "dashboard");
  }, [open, sourceId]);
  const scope = scopes.find((candidate) => candidate.key === scopeKey) ?? scopes[0];
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // The text is the stored one each time the dialog opens, the scope changes or a new revision lands.
  useEffect(() => {
    if (!open) return;
    setText(scope.path === null ? sourceText(dashboard.manifest ?? dashboard.spec, null) : sourceText(dashboard.spec, scope.path));
    setError(null);
  }, [open, scope.path, dashboard.spec, dashboard.manifest]);

  const save = () => {
    const parsed = parseSource(text);
    if (!parsed.ok) {
      setError(t_i18n("This is not valid JSON: {reason}", { reason: parsed.error }));
      return;
    }
    const done = () => {
      setSaving(false);
      toast({ title: t_i18n("Definition saved"), variant: "success" });
      onOpenChange(false);
    };
    const failed = (failure: Error & { status?: number }) => {
      setSaving(false);
      setError(
        failure.status === 409
          ? t_i18n("This dashboard changed meanwhile. Close the editor to see its latest version.")
          : failure.message,
      );
    };
    setSaving(true);
    setError(null);
    if (scope.path === null) {
      replace.mutate({ revision: dashboard.revision, definition: parsed.value }, { onSuccess: done, onError: failed });
    } else {
      update.edit([{ op: "set", path: scope.path, value: parsed.value }], { onSuccess: done, onError: failed });
    }
  };

  const label = (candidate: SourceScope) =>
    candidate.id ? `${t_i18n(SCOPE_LABELS[candidate.kind])} · ${candidate.id}` : t_i18n(SCOPE_LABELS[candidate.kind]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90dvh] max-w-4xl flex-col gap-4" data-testid="dashboard.source">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Braces className="h-4 w-4 text-accent" aria-hidden="true" />
            {t_i18n("Dashboard source")}
          </DialogTitle>
          <DialogDescription>
            {scope.path === null
              ? t_i18n(
                  "The dashboard as an OpenCTI manifest: OpenCTI and OpenAEV widgets as their platform stores them, and under x_xtm_one what XTM One adds - its own widgets, data sources, inputs, tabs. It is checked when you save.",
                )
              : t_i18n("The definition as JSON: data sources, visualizations, inputs, tabs and their layouts. It is checked when you save.")}
          </DialogDescription>
        </DialogHeader>
        {scopes.length > 1 && (
          <SegmentedFilter
            ariaLabel={t_i18n("What to edit")}
            value={scope.key}
            onChange={setScopeKey}
            options={scopes.map((candidate) => ({ key: candidate.key, label: label(candidate) }))}
          />
        )}
        <CodeEditor
          value={text}
          language="json"
          onChange={setText}
          disabled={!dashboard.can_manage || saving}
          ariaLabel={label(scope)}
          className="min-h-80 flex-1"
        />
        {error && (
          <p role="alert" className="content-compact text-feedback-error-primary" data-testid="dashboard.source.error">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t_i18n("Close")}
          </Button>
          {dashboard.can_manage && (
            <Button onClick={save} disabled={saving} aria-busy={saving || undefined}>
              {saving && <Spinner size="sm" tone="inherit" />}
              {t_i18n("Save")}
            </Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
