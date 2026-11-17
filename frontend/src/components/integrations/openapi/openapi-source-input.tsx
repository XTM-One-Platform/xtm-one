/**
 * Where an OpenAPI / Swagger document comes from: a URL the server fetches, a
 * `.json` / `.yaml` / `.yml` file read here and sent as text, or a `.zip` of a
 * spec split across files, sent base64-encoded (the same JSON body serves
 * preview, create and replace — no multipart).
 *
 * Loading runs the preview endpoint; the parent receives the parsed preview
 * together with the source, which it sends again on create.
 */
import { useRef, useState } from "react";
import { FileUp, Link2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { usePreviewOpenAPISpec } from "@/hooks/api/useIntegrationApi";
import { useFormatter } from "@/i18n";
import type { OpenAPIPreview, OpenAPISourceRequest } from "@/types";
import { Spinner } from "@filigran/design-system";

/** Same ceilings as the backend (10 MB for a document, 20 MB for an archive). */
const MAX_SPEC_BYTES = 10 * 1024 * 1024;
const MAX_ARCHIVE_BYTES = 20 * 1024 * 1024;
const ACCEPT = ".json,.yaml,.yml,.zip,application/json,application/yaml,application/x-yaml,text/yaml,text/plain,application/zip";

type Mode = "url" | "file";

export type OpenAPISource = Pick<OpenAPISourceRequest, "url" | "content" | "archive" | "filename">;

function isArchive(file: File): boolean {
  return file.name.toLowerCase().endsWith(".zip") || file.type === "application/zip";
}

/** A file's bytes as base64 (without the data-URL prefix). */
function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

export function OpenAPISourceInput({
  onLoaded,
  initialUrl,
}: {
  onLoaded: (preview: OpenAPIPreview, source: OpenAPISource) => void;
  initialUrl?: string;
}) {
  const { t_i18n } = useFormatter();
  const preview = usePreviewOpenAPISpec();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [mode, setMode] = useState<Mode>("url");
  const [url, setUrl] = useState(initialUrl ?? "");
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setError(null);
    let source: OpenAPISource;
    if (mode === "url") {
      if (!url.trim()) return;
      source = { url: url.trim() };
    } else {
      if (!file) return;
      if (isArchive(file)) {
        if (file.size > MAX_ARCHIVE_BYTES) {
          setError(t_i18n("The archive exceeds the maximum size of 20 MB."));
          return;
        }
        source = { archive: await readAsBase64(file), filename: file.name };
      } else {
        if (file.size > MAX_SPEC_BYTES) {
          setError(t_i18n("The file exceeds the maximum size of 10 MB."));
          return;
        }
        source = { content: await file.text(), filename: file.name };
      }
    }
    try {
      const result = await preview.mutateAsync(source);
      onLoaded(result, source);
    } catch (err) {
      setError(err instanceof Error ? err.message : t_i18n("The spec could not be loaded."));
    }
  };

  const canLoad = mode === "url" ? url.trim().length > 0 : file !== null;

  return (
    <div className="space-y-3">
      <SegmentedFilter<Mode>
        ariaLabel={t_i18n("Spec source")}
        options={[
          { key: "url", label: "From a URL" },
          { key: "file", label: "Upload a file" },
        ]}
        value={mode}
        onChange={(next) => { setMode(next); setError(null); }}
        className="w-fit"
      />

      <div className="flex gap-2">
        {mode === "url" ? (
          <div className="relative flex-1">
            <Link2 className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-default-secondary" />
            <Input aria-label={t_i18n("Spec source")}
              className="pl-8 font-mono-plex content-compact"
              placeholder="https://api.example.com/openapi.json"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void load(); } }}
            />
          </div>
        ) : (
          <>
            <input
              ref={fileRef}
              type="file"
              accept={ACCEPT}
              className="hidden"
              onChange={(e) => {
                setFile(e.target.files?.[0] ?? null);
                setError(null);
                e.currentTarget.value = "";
              }}
            />
            <Button variant="secondary" className="gap-1.5" onClick={() => fileRef.current?.click()}>
              <FileUp className="h-3.5 w-3.5" />
              {t_i18n("Choose file")}
            </Button>
            <div className="flex flex-1 items-center truncate rounded-sm border border-dashed border-elevation-subtle-soft px-3 content-compact text-default-secondary">
              {file ? file.name : t_i18n("JSON or YAML (.json, .yaml, .yml), or a .zip of a spec split across files")}
            </div>
          </>
        )}
        <Button onClick={() => void load()} disabled={!canLoad || preview.isPending} className="gap-1.5" aria-busy={preview.isPending || undefined}>
          {preview.isPending && <Spinner size="sm" tone="inherit" />}
          {preview.isPending ? t_i18n("Loading...") : t_i18n("Load spec")}
        </Button>
      </div>

      <p className="text-[11px] text-default-secondary">
        {mode === "url"
          ? t_i18n("Swagger 2.0 or OpenAPI 3.x, JSON or YAML. Public and private-network hosts are reachable; point to the raw document, not the Swagger UI page.")
          : t_i18n("Swagger 2.0 or OpenAPI 3.x, JSON or YAML, up to 10 MB. For a spec split across files, upload a .zip of all of them.")}
      </p>

      {error && (
        <div className="rounded-sm border border-feedback-error-primary/20 bg-feedback-error-primary/10 px-3 py-2 content-compact text-feedback-error-primary">{error}</div>
      )}
    </div>
  );
}
