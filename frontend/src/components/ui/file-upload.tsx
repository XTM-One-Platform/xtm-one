// SPDX-License-Identifier: Apache-2.0

import { useCallback, useId, useRef, useState } from "react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { InfoTip } from "@/components/ui/info-tip";
import { Label } from "@/components/ui/label";
import { dataUrlByteSize, readFileAsDataUrl } from "@/lib/file-utils";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

const ACCEPT_IMAGE = "image/png,image/jpeg,image/webp,image/x-icon,image/vnd.microsoft.icon,image/gif,.ico";

interface FileUploadProps {
  /** Field label shown above the uploader; it names the drop zone and the file input. */
  label: string;
  /** Helper text shown below the label. */
  help?: string;
  /** The bubble beside the label: what the image is for, its format and size, what happens when empty. */
  info?: React.ReactNode;
  /** The id of the file input; generated when omitted. */
  id?: string;
  /** The name of the file input (the setting it fills). */
  name?: string;
  /** Current value — base64 data URL or http(s) URL, or null if unset. */
  value: string | null;
  /** When true, interaction is disabled. */
  disabled?: boolean;
  /** Maximum decoded byte size; default 512 KB. */
  maxBytes?: number;
  /** Height in px of the image preview; default 40. */
  previewHeight?: number;
  /** The `accept` attribute of the file chooser (a list of MIME types); defaults to common image formats. */
  accept?: string;
  /** Called with the new data URL when a file is picked, or null when removed. */
  onChange: (v: string | null) => void;
  /** Called with a human-readable message when a file is rejected. */
  onError?: (msg: string) => void;
}

export function FileUpload({
  label,
  help,
  info,
  id,
  name,
  value,
  disabled = false,
  maxBytes = 512 * 1024,
  previewHeight = 40,
  accept = ACCEPT_IMAGE,
  onChange,
  onError,
}: FileUploadProps) {
  const { t_i18n } = useFormatter();
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const labelId = `${inputId}-label`;
  const helpId = `${inputId}-help`;
  const limitId = `${inputId}-limit`;
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragging, setDragging] = useState(false);

  const processFile = useCallback(async (file: File) => {
    if (file.size > maxBytes) {
      onError?.(t_i18n("File is {size} KB; the limit is {limit} KB.", { size: Math.round(file.size / 1024), limit: Math.round(maxBytes / 1024) }));
      return;
    }
    try {
      const dataUrl = await readFileAsDataUrl(file);
      if (accept.includes("image/") && !dataUrl.startsWith("data:image/")) {
        onError?.(t_i18n("Only image files are accepted."));
        return;
      }
      if (dataUrl.toLowerCase().startsWith("data:image/svg+xml") && !accept.includes("image/svg+xml")) {
        onError?.(t_i18n("SVG files are not accepted for this field."));
        return;
      }
      if (dataUrlByteSize(dataUrl) > maxBytes) {
        onError?.(t_i18n("Encoded payload exceeds {limit} KB.", { limit: Math.round(maxBytes / 1024) }));
        return;
      }
      onChange(dataUrl);
    } catch {
      onError?.(t_i18n("Failed to read the file."));
    }
  }, [accept, maxBytes, onChange, onError, t_i18n]);

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0] ?? null;
    if (file) void processFile(file);
    e.target.value = "";
  };

  const handleDragOver = (e: React.DragEvent) => {
    if (disabled) return;
    e.preventDefault();
    setDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    // Only clear when leaving the drop zone entirely (not a child element)
    if (!e.currentTarget.contains(e.relatedTarget as Node)) {
      setDragging(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (disabled) return;
    const file = e.dataTransfer.files?.[0];
    if (file) void processFile(file);
  };

  return (
    <div>
      {/* The same field pattern as SettingsField: label and info bubble, then
          the helper; both name and describe the drop zone and the file input. */}
      <div className="mb-1.5 flex items-center gap-1.5">
        <Label id={labelId} htmlFor={inputId} className="text-default-primary">
          {label}
        </Label>
        {info && <InfoTip content={<div className="max-w-xs space-y-1 content-compact">{info}</div>} label={t_i18n("About this field")} />}
      </div>
      {help && <p id={helpId} className="mb-2 content-compact text-default-secondary">{help}</p>}

      <div
        className={cn(
          "relative rounded-sm border-2 border-dashed transition-colors",
          dragging
            ? "border-accent bg-accent/5"
            : "border-elevation-subtle-soft layer-1 bg-elevation-default hover:border-elevation-subtle",
          disabled && "pointer-events-none opacity-60",
        )}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        <input
          ref={inputRef}
          id={inputId}
          name={name}
          type="file"
          accept={accept}
          aria-describedby={help ? helpId : undefined}
          className="hidden"
          onChange={handleInputChange}
          disabled={disabled}
        />

        {value ? (
          /* ── Filled state ─────────────────────────────────── */
          <div className="flex items-center gap-3 p-3">
            <div className="shrink-0 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-1.5">
              {/* Keyed by its source: a replacement gets a fresh <img>, never
                  the one a failed preview hid. */}
              <img
                key={value}
                src={value}
                alt={`${label} preview`}
                style={{ height: previewHeight }}
                className="w-auto object-contain"
                onError={(e) => {
                  (e.target as HTMLImageElement).style.display = "none";
                }}
              />
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate content-compact text-default-secondary">
                {value.startsWith("data:") ? t_i18n("Uploaded file") : value}
              </p>
              <p className="mt-0.5 content-compact text-default-secondary">
                {t_i18n("Drop a new file to replace")}
              </p>
            </div>
            <div className="flex shrink-0 gap-1.5">
              <Button
                type="button"
                variant="outline"
                disabled={disabled}
                aria-describedby={labelId}
                onClick={() => inputRef.current?.click()}
              >
                <Icon name="upload" size={16} className="mr-1" />
                {t_i18n("Replace")}
              </Button>
              <Button aria-label={t_i18n("Remove file")}
                type="button"
                variant="ghost"
                size="icon"
                disabled={disabled}
                onClick={() => onChange(null)}
              >
                <Icon name="x" size={16} />
              </Button>
            </div>
          </div>
        ) : (
          /* ── Empty / drop state ───────────────────────────── */
          <button
            type="button"
            className="flex w-full flex-col items-center gap-2 px-4 py-6 text-center"
            aria-labelledby={labelId}
            aria-describedby={[help ? helpId : null, limitId].filter(Boolean).join(" ")}
            disabled={disabled}
            onClick={() => inputRef.current?.click()}
          >
            <div
              className={cn(
                "flex items-center justify-center rounded-full border p-2 transition-colors",
                dragging
                  ? "border-accent/50 bg-accent/10 text-accent"
                  : "border-elevation-subtle-soft bg-elevation-highlight text-default-secondary",
              )}
            >
              {dragging ? (
                <Icon name="upload" size={16} />
              ) : (
                <Icon name="image" size={16} />
              )}
            </div>
            <div>
              <p className="content-base font-medium text-default-secondary">
                {dragging ? t_i18n("Drop to upload") : t_i18n("Click or drag & drop")}
              </p>
              <p id={limitId} className="mt-0.5 content-compact text-default-secondary">
                {t_i18n("Max {limit} KB", { limit: Math.round(maxBytes / 1024) })}
              </p>
            </div>
          </button>
        )}

        {/* Drag-over overlay glow */}
        {dragging && (
          <div className="pointer-events-none absolute inset-0 rounded-sm ring-2 ring-accent/50" />
        )}
      </div>
    </div>
  );
}
