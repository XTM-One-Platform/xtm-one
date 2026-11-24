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
 * CSV import dialog for assets and exposure (#11543 S3).
 *
 * Two steps: pick a file (preview), then apply. The preview shows the parsed
 * rows, the errors and the primary asset links the file names. Apply saves and
 * closes; a row refused is shown inline and the Apply button is disabled.
 */

import { useCallback, useRef, useState } from "react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Spinner } from "@filigran/design-system";
import {
  type AssetsImportResponse,
  useImportOrganizationAssets,
} from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { OrganizationSharedContextNotice } from "./OrganizationSharedContextNotice";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function OrganizationAssetsImportDialog({ open, onOpenChange }: Props) {
  const { t_i18n } = useFormatter();
  const importMutation = useImportOrganizationAssets();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [preview, setPreview] = useState<AssetsImportResponse | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reset = useCallback(() => {
    setPreview(null);
    setFile(null);
    setError(null);
    importMutation.reset();
  }, [importMutation]);

  const close = useCallback(() => {
    onOpenChange(false);
    reset();
  }, [onOpenChange, reset]);

  const onFileSelected = useCallback(
    async (selected: File) => {
      setFile(selected);
      setError(null);
      try {
        const result = await importMutation.mutateAsync({ file: selected, apply: false });
        setPreview(result);
      } catch {
        // A catalogued sentence, never the backend's text.
        setError(t_i18n("The file could not be read."));
      }
    },
    [importMutation, t_i18n],
  );

  const apply = useCallback(async () => {
    if (!file) return;
    setError(null);
    try {
      await importMutation.mutateAsync({ file, apply: true });
      close();
    } catch {
      setError(t_i18n("The import failed."));
    }
  }, [file, importMutation, close, t_i18n]);

  const hasErrors = (preview?.errors.length ?? 0) > 0;
  const assetCount = preview?.assets.length ?? 0;

  return (
    <Dialog open={open} onOpenChange={(v) => (v ? onOpenChange(true) : close())}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{t_i18n("Import assets from CSV")}</DialogTitle>
          <DialogDescription>
            {t_i18n("Upload a CSV with columns: name, type, environment, internet_facing, owner, group, crown_jewels. Rows are upserted by name.")}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          {/* #11994: a CSV reads no platform, so only the shared-visibility sentence. */}
          <OrganizationSharedContextNotice kind="visibility" elevation={1} />
          {!preview && (
            <div className="flex flex-col items-center gap-3 rounded-sm border border-dashed border-elevation-subtle p-6">
              <Icon name="upload" size={24} className="text-default-secondary" />
              <Button
                variant="secondary"
                className="gap-1.5"
                disabled={importMutation.isPending}
                aria-busy={importMutation.isPending || undefined}
                onClick={() => inputRef.current?.click()}
              >
                {importMutation.isPending && <Spinner size="sm" tone="inherit" />}
                {t_i18n("Choose a CSV file")}
              </Button>
              <input
                ref={inputRef}
                type="file"
                accept=".csv,text/csv"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) onFileSelected(f);
                  e.target.value = "";
                }}
              />
            </div>
          )}

          {preview && (
            <>
              <p className="content-base text-default-primary">
                {t_i18n("{count, plural, one {# asset parsed} other {# assets parsed}}", { count: assetCount })}
                {preview.replaced.length > 0 &&
                  ` (${t_i18n("{count, plural, one {# existing asset replaced} other {# existing assets replaced}}", { count: preview.replaced.length })})`}
              </p>

              {Object.keys(preview.links).length > 0 && (
                <div className="space-y-1">
                  <p className="content-compact font-semibold text-default-primary">{t_i18n("Primary asset links")}</p>
                  <ul className="list-disc space-y-0.5 pl-5 content-compact text-default-secondary">
                    {Object.entries(preview.links).map(([jewel, assets]) => (
                      <li key={jewel}>
                        {jewel}: {assets.join(", ")}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {preview.unknown_crown_jewels.length > 0 && (
                <p className="content-compact text-feedback-warning-primary">
                  {t_i18n("Unknown primary assets (ignored): {names}", { names: preview.unknown_crown_jewels.join(", ") })}
                </p>
              )}

              {hasErrors && (
                <div className="space-y-1">
                  <p className="content-compact font-semibold text-feedback-error-primary">{t_i18n("Errors")}</p>
                  <ul className="list-disc space-y-0.5 pl-5 content-compact text-feedback-error-primary">
                    {preview.errors.map((err) => (
                      <li key={`${err.line}-${err.name}`}>
                        {t_i18n("Line {line} ({name}): {message}", { line: err.line, name: err.name, message: err.message })}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}

          {error && <p className="content-compact text-feedback-error-primary">{error}</p>}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={close}>
            {t_i18n("Cancel")}
          </Button>
          {preview && (
            <Button
              className="gap-1.5"
              disabled={hasErrors || importMutation.isPending || assetCount === 0}
              onClick={apply}
              aria-busy={importMutation.isPending || undefined}
            >
              {importMutation.isPending && <Spinner size="sm" tone="inherit" />}
              {t_i18n("Apply")}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
