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

import { useState, useEffect, useCallback, useRef, useId } from "react";
import { Play, CheckCircle2, XCircle, Clock, ChevronDown, ChevronRight, Paperclip, FileText, X } from "lucide-react";
import { Spinner } from "@filigran/design-system";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { JsonSchema } from "@/types";

// ---------------------------------------------------------------------------
// ToolTestDialog — reusable dialog for testing any tool (MCP, integration, custom)
// ---------------------------------------------------------------------------

interface ToolTestResult {
  status: string;
  output?: string;
  message?: string;
  duration_ms?: number;
}

interface ToolTestDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  toolName: string;
  toolDescription?: string;
  inputSchema?: JsonSchema | null;
  /** Called with the parsed arguments when the user clicks Run. Should return the result. */
  onRun: (args: Record<string, unknown>) => Promise<ToolTestResult>;
  /** When provided, enables file upload for testing. Files are injected as $files. */
  onUploadFile?: (file: File) => Promise<{ file_id: string; filename: string; content_type: string; size: number }>;
  /** Called when the dialog closes to clean up uploaded test files. */
  onCleanupFiles?: (fileIds: string[]) => void;
  /** Offer a dry-run switch: the run receives `dry_run: true` (a custom tool's contract). */
  dryRunOption?: boolean;
}

interface UploadedFile {
  file_id: string;
  filename: string;
  content_type: string;
  size: number;
}

/** Generate a template JSON string from a JSON Schema (top-level properties only).
 *  Uses the schema `default` value when available, otherwise falls back to a
 *  sensible zero-value for the declared type. */
function schemaTemplate(schema?: JsonSchema | null): string {
  if (!schema?.properties) return "{}";

  const template: Record<string, unknown> = {};
  for (const [key, def] of Object.entries(schema.properties)) {
    if (!def) {
      template[key] = "";
      continue;
    }
    const defWithDefault = def as { type?: string; description?: string; default?: unknown };
    if ("default" in defWithDefault && defWithDefault.default !== undefined) {
      template[key] = defWithDefault.default;
      continue;
    }
    switch (def.type) {
      case "string":
        template[key] = "";
        break;
      case "number":
      case "integer":
        template[key] = 0;
        break;
      case "boolean":
        template[key] = false;
        break;
      case "array":
        template[key] = [];
        break;
      case "object":
        template[key] = {};
        break;
      default:
        template[key] = "";
    }
  }
  return JSON.stringify(template, null, 2);
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function ToolTestDialog({
  open,
  onOpenChange,
  toolName,
  toolDescription,
  inputSchema,
  onRun,
  onUploadFile,
  onCleanupFiles,
  dryRunOption = false,
}: ToolTestDialogProps) {
  const { t_i18n } = useFormatter();
  const argsId = useId();
  const filesLabelId = useId();
  const filesHelpId = useId();
  const addFileId = useId();
  const dryRunId = useId();
  const [args, setArgs] = useState("{}");
  const [dryRun, setDryRun] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ToolTestResult | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const [showSchema, setShowSchema] = useState(false);
  const [uploadedFiles, setUploadedFiles] = useState<UploadedFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const prevOpenRef = useRef(open);

  useEffect(() => {
    if (open) {
      setArgs(schemaTemplate(inputSchema));
      setResult(null);
      setParseError(null);
      setRunning(false);
      setShowSchema(false);
      setUploadedFiles([]);
      setUploading(false);
      setDryRun(false);
    }
  }, [open, toolName, inputSchema]);

  useEffect(() => {
    if (prevOpenRef.current && !open) {
      const fileIds = uploadedFiles.map((f) => f.file_id);
      if (fileIds.length > 0 && onCleanupFiles) {
        onCleanupFiles(fileIds);
      }
    }
    prevOpenRef.current = open;
  }, [open, uploadedFiles, onCleanupFiles]);

  const handleFileUpload = useCallback(async (file: File) => {
    if (!onUploadFile) return;
    setUploading(true);
    try {
      const result = await onUploadFile(file);
      setUploadedFiles((prev) => [...prev, result]);
    } catch {
      // Upload failed
    } finally {
      setUploading(false);
    }
  }, [onUploadFile]);

  const handleRemoveFile = useCallback((fileId: string) => {
    setUploadedFiles((prev) => prev.filter((f) => f.file_id !== fileId));
  }, []);

  const handleRun = useCallback(async () => {
    setParseError(null);
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(args);
    } catch {
      setParseError("Invalid JSON. Please check your input.");
      return;
    }
    // Inject uploaded test file IDs so the backend can build $files
    if (uploadedFiles.length > 0) {
      parsed["$file_ids"] = uploadedFiles.map((f) => f.file_id);
    }
    if (dryRunOption && dryRun) {
      parsed["dry_run"] = true;
    }
    setRunning(true);
    setResult(null);
    try {
      const res = await onRun(parsed);
      setResult(res);
    } catch (err: unknown) {
      setResult({ status: "error", message: err instanceof Error ? err.message : "Unknown error" });
    } finally {
      setRunning(false);
    }
  }, [args, onRun, uploadedFiles, dryRunOption, dryRun]);

  const hasProperties = !!(inputSchema?.properties && Object.keys(inputSchema.properties).length > 0);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg max-h-[85vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="text-base flex items-center gap-2">
            {t_i18n("Test Tool")}
          </DialogTitle>
          <DialogDescription className="space-y-1.5">
            <code className="rounded-sm bg-accent/10 px-1.5 py-0.5 text-[11px] font-mono-plex text-accent">
              {toolName}
            </code>
            {toolDescription && (
              <p className="content-compact text-default-secondary leading-relaxed mt-1">{toolDescription}</p>
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto space-y-4 pr-1">
          {/* A tool with no declared parameters is normal; a whole catalog of
              them usually is not. It is what an MCP server returns when
              `tools/list` was answered before authorization, so say that
              rather than showing an empty `{}` with nothing to fill in. */}
          {!hasProperties && (
            <p className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-3 text-[11px] text-default-secondary leading-relaxed">
              {t_i18n("This tool declares no input parameters. If you expected some, the server may have listed its tools before authorization: re-run Test Connection once the server is authorized to refresh the schema.")}
            </p>
          )}
          {hasProperties && (
            <div>
              <button
                type="button"
                className="flex items-center gap-1.5 text-[11px] text-default-secondary hover:text-default-primary transition-colors"
                onClick={() => setShowSchema(!showSchema)}
              >
                {showSchema ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                {t_i18n("Input Schema Reference")}
              </button>
              {showSchema && (
                <pre className="mt-1.5 rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-3 text-[11px] font-mono-plex text-default-secondary overflow-x-auto max-h-40 whitespace-pre-wrap break-all">
                  {JSON.stringify(inputSchema, null, 2)}
                </pre>
              )}
            </div>
          )}

          {/* Attach test files (injected as $files at runtime) */}
          {!!onUploadFile && (
            <div className="space-y-1.5">
              <Label id={filesLabelId}>{t_i18n("Test Files")}</Label>
              <p id={filesHelpId} className="content-caption text-default-secondary">
                {t_i18n("available as")} <code className="text-accent">$files</code> {t_i18n("in tool code")}
              </p>
              {uploadedFiles.length > 0 && (
                <div className="space-y-1">
                  {uploadedFiles.map((f) => (
                    <div key={f.file_id} className="flex items-center gap-2 rounded-sm bg-elevation-hover border border-elevation-subtle-soft px-2.5 py-1.5 content-compact">
                      <FileText className="h-3.5 w-3.5 text-accent shrink-0" />
                      <span className="truncate text-default-secondary">{f.filename}</span>
                      <span className="text-default-secondary content-caption">{formatFileSize(f.size)}</span>
                      <button aria-label={t_i18n("Remove file")}
                        type="button"
                        className="ml-auto p-0.5 rounded-sm hover:bg-elevation-hover transition-colors"
                        onClick={() => handleRemoveFile(f.file_id)}
                      >
                        <X className="h-3 w-3 text-default-secondary" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <div className="flex items-center gap-2">
                <input
                  type="file"
                  ref={fileInputRef}
                  className="hidden"
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) handleFileUpload(f);
                    if (fileInputRef.current) fileInputRef.current.value = "";
                  }}
                />
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="gap-1.5 content-compact h-7"
                  disabled={uploading}
                  id={addFileId}
                  aria-labelledby={`${filesLabelId} ${addFileId}`}
                  aria-describedby={filesHelpId}
                  aria-busy={uploading || undefined}
                  onClick={() => fileInputRef.current?.click()}
                >
                  {uploading && <Spinner size="sm" tone="inherit" />}
                  {!uploading && <Paperclip className="h-3 w-3" />}
                  {uploading ? t_i18n("Uploading...") : t_i18n("Add file")}
                </Button>
              </div>
            </div>
          )}

          {dryRunOption && (
            <div className="flex items-start justify-between gap-3 rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-3">
              <div className="space-y-0.5">
                <Label htmlFor={dryRunId}>{t_i18n("Dry run")}</Label>
                <p className="content-caption text-default-secondary">
                  {t_i18n("Runs the tool with dry_run set to true. The tool must check it and skip its changes: the sandbox cannot do it for the tool.")}
                </p>
              </div>
              <Switch id={dryRunId} checked={dryRun} onCheckedChange={setDryRun} />
            </div>
          )}

          {/* Arguments input */}
          <div className="space-y-1.5">
            <Label htmlFor={argsId}>{t_i18n("Arguments (JSON)")}</Label>
            <textarea
              id={argsId}
              value={args}
              onChange={(e) => { setArgs(e.target.value); setParseError(null); }}
              className={cn(
                "w-full rounded-sm bg-elevation-hover border px-3 py-2.5 font-mono-plex content-compact text-default-primary placeholder:text-default-secondary",
                "focus:outline-hidden focus-visible:ring-2 focus-visible:ring-focus focus:border-accent/30",
                "resize-y min-h-[100px] max-h-[200px]",
                parseError ? "border-feedback-error-primary/50" : "border-elevation-subtle-soft",
              )}
              placeholder='{ "key": "value" }'
              spellCheck={false}
            />
            {parseError && (
              <p className="text-[11px] text-feedback-error-primary">{t_i18n(parseError)}</p>
            )}
          </div>

          {/* Result area */}
          {result && (
            <div className={cn(
              "rounded-sm border p-3 space-y-2",
              result.status === "ok"
                ? "bg-feedback-success-primary/[0.05] border-feedback-success-primary/20"
                : "bg-feedback-error-primary/[0.05] border-feedback-error-primary/20",
            )}>
              <div className="flex items-center gap-2">
                {result.status === "ok" ? (
                  <CheckCircle2 className="h-4 w-4 text-feedback-success-primary" />
                ) : (
                  <XCircle className="h-4 w-4 text-feedback-error-primary" />
                )}
                <span className={cn("content-compact font-medium", result.status === "ok" ? "text-feedback-success-primary" : "text-feedback-error-primary")}>
                  {result.status === "ok" ? t_i18n("Success") : t_i18n("Failed")}
                </span>
                {result.duration_ms !== undefined && (
                  <span className="ml-auto flex items-center gap-1 content-caption text-default-secondary">
                    <Clock className="h-3 w-3" />
                    {result.duration_ms}ms
                  </span>
                )}
              </div>
              <pre className="content-compact font-mono-plex text-default-secondary whitespace-pre-wrap break-words max-h-[200px] overflow-y-auto">
                {result.status === "ok"
                  ? (result.output || t_i18n("(no output)"))
                  : (result.message || t_i18n("Execution failed"))}
              </pre>
            </div>
          )}
        </div>

        {/* Actions */}
        <div className="flex justify-end gap-2 pt-3 border-t border-elevation-subtle-soft">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onOpenChange(false)}
          >
            {t_i18n("Close")}
          </Button>
          <Button
            size="sm"
            className="gap-1.5"
            onClick={handleRun}
            disabled={running || uploading}
            aria-busy={running || undefined}
          >
            {running && <Spinner size="sm" tone="inherit" />}
            {!running && <Play className="h-3.5 w-3.5" />}
            {running ? t_i18n("Running...") : t_i18n("Run")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
