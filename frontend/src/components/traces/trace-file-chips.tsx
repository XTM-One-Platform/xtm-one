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

import { Download, FileText } from "lucide-react";
import { Tip } from "@/components/ui/tooltip";
import { API_BASE } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { TraceFileRef } from "@/lib/trace-utils";

function formatFileSize(bytes?: number): string {
  if (!bytes || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function TraceFileChips({
  files,
  className,
}: {
  files: TraceFileRef[];
  className?: string;
}) {
  if (files.length === 0) return null;

  return (
    <div className={cn("flex flex-wrap gap-1.5", className)}>
      {files.map((file) => {
        const sizeLabel = formatFileSize(file.size);
        return (
          <Tip
            key={file.file_id}
            content={file.content_type ? `${file.filename} (${file.content_type})` : file.filename}
          >
            <a
              href={`${API_BASE}/chat/files/${file.file_id}/download`}
              target="_blank"
              rel="noopener noreferrer"
              className="group inline-flex max-w-full items-center gap-1.5 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2 py-1 content-compact text-default-secondary hover:border-accent/40 hover:bg-accent/[0.06]"
            >
              <FileText className="h-3.5 w-3.5 shrink-0 text-default-secondary group-hover:text-accent" />
              <span className="truncate">{file.filename}</span>
              {sizeLabel ? (
                <span className="shrink-0 content-caption text-default-secondary">{sizeLabel}</span>
              ) : null}
              <Download className="h-3 w-3 shrink-0 text-default-secondary group-hover:text-accent" />
            </a>
          </Tip>
        );
      })}
    </div>
  );
}
