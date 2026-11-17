// SPDX-License-Identifier: Apache-2.0

/**
 * DocumentsPicker: the shared control for attaching several documents to a
 * form field (any file type, a count and a per-file size limit). A 36 px
 * secondary "Add documents" button drives a hidden native input, so no browser
 * chrome ("Choose Files", "No file chosen") or untranslated text reaches the
 * page; the files picked are listed below with their size and a remove button.
 * The single-image variant stays `FileUpload` (file-upload.tsx).
 */
import { useRef } from "react";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";

interface DocumentsPickerProps {
  /** Id of the hidden input, so a field label can point at it. */
  id: string;
  /** The files picked so far, in the order they were added. */
  files: readonly File[];
  /** Called with the new list after an add or a remove. */
  onChange: (files: File[]) => void;
  /** Called with a sentence when a file is refused (too large, too many), or null when every file was kept. */
  onError?: (message: string | null) => void;
  /** At most this many files; the button is disabled once reached. */
  maxFiles: number;
  /** Per-file limit in bytes. */
  maxBytes: number;
  /** The file input's accept attribute (MIME types or extensions); default every type. */
  accept?: string;
  disabled?: boolean;
  "aria-labelledby"?: string;
  "aria-describedby"?: string;
}

/** A byte count for a file list, in the person's locale: KB under 1 MB, MB above. */
function formatFileSize(bytes: number, locale?: string): string {
  const mb = 1024 * 1024;
  if (bytes >= mb) return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(bytes / mb)} MB`;
  return `${new Intl.NumberFormat(locale).format(Math.max(1, Math.round(bytes / 1024)))} KB`;
}

/** The list after adding `incoming`, and the first refusal to show, if any. */
function addDocuments(
  current: readonly File[],
  incoming: readonly File[],
  maxFiles: number,
  maxBytes: number,
): { files: File[]; tooLarge: File | null; overCount: boolean } {
  const tooLarge = incoming.find((file) => file.size > maxBytes) ?? null;
  const kept = incoming.filter((file) => file.size <= maxBytes);
  const files = [...current, ...kept].slice(0, maxFiles);
  return { files, tooLarge, overCount: current.length + kept.length > maxFiles };
}

export function DocumentsPicker({
  id,
  files,
  onChange,
  onError,
  maxFiles,
  maxBytes,
  accept,
  disabled = false,
  ...aria
}: DocumentsPickerProps) {
  const { t_i18n, locale } = useFormatter();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const full = files.length >= maxFiles;

  const add = (list: FileList | null) => {
    if (!list) return;
    const { files: next, tooLarge, overCount } = addDocuments(files, Array.from(list), maxFiles, maxBytes);
    onChange(next);
    onError?.(
      tooLarge
        ? t_i18n("{file} is over {limit} MB and was not added.", { file: tooLarge.name, limit: Math.round(maxBytes / (1024 * 1024)) })
        : overCount
          ? t_i18n("At most {count} documents: the others were not added.", { count: maxFiles })
          : null,
    );
  };

  return (
    <div className="space-y-2">
      <input
        ref={inputRef}
        id={id}
        type="file"
        multiple
        accept={accept}
        className="hidden"
        tabIndex={-1}
        aria-hidden
        aria-labelledby={aria["aria-labelledby"]}
        disabled={disabled || full}
        onChange={(e) => {
          add(e.target.files);
          e.target.value = "";
        }}
      />
      <Button
        type="button"
        variant="secondary"
        className="gap-1.5"
        disabled={disabled || full}
        aria-labelledby={aria["aria-labelledby"]}
        aria-describedby={aria["aria-describedby"]}
        onClick={() => inputRef.current?.click()}
      >
        <Icon name="paperclip" size={16} />
        {t_i18n("Add documents")}
      </Button>
      {files.length > 0 && (
        <ul className="space-y-1" data-testid={`${id}.list`}>
          {files.map((file, index) => (
            <li key={`${file.name}-${index}`} className="flex items-center gap-2 content-base">
              <Icon name="file-text" size={16} className="shrink-0 text-default-secondary" />
              <span className="min-w-0 flex-1 truncate">{file.name}</span>
              <span className="content-compact text-default-secondary">{formatFileSize(file.size, locale)}</span>
              <Tip content={t_i18n("Remove {file}", { file: file.name })}>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8"
                  aria-label={t_i18n("Remove {file}", { file: file.name })}
                  disabled={disabled}
                  onClick={() => {
                    onChange(files.filter((_, i) => i !== index));
                    onError?.(null);
                  }}
                >
                  <Icon name="x" size={16} />
                </Button>
              </Tip>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
