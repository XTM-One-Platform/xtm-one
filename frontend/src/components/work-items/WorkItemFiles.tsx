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
 * The files a work item carries (#12092): attached by its creator, by an
 * update while it runs (a review's captures) or by its completion. One row
 * per file, in the order attached: its type icon, name, type and size, and
 * a download button. Files no longer stored are counted on one line below.
 * A long name is cut to the row by the shared TruncatedText (UX r1 B1 on
 * #12103): its tooltip and its single tab stop exist only when it is cut.
 */
import { useCallback } from "react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { downloadErrorDescription } from "@/lib/download-error";
import { cn } from "@/lib/utils";
import {
  authenticatedDownload,
  formatFileSize,
  mimeLabel,
  mimeVisual,
  extensionFromFilename,
  resolveAttachmentMime,
} from "@/pages/chat/fileVisuals";
import type { WorkItemAttachedFile, WorkItemAttachments } from "@/types";

function FileRow({ file }: { file: WorkItemAttachedFile }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const visual = { type: extensionFromFilename(file.filename), content_type: file.content_type };
  const mime = resolveAttachmentMime(visual);
  const { icon: TypeIcon, color } = mimeVisual(mime);
  const facts = [mimeLabel(visual, mime), formatFileSize(file.size)].filter(Boolean);
  const download = useCallback(async () => {
    try {
      await authenticatedDownload(`/api/v1/chat/files/${file.file_id}/download`, file.filename);
    } catch (err) {
      toast({
        title: t_i18n("Download failed"),
        description: downloadErrorDescription(err, file.filename, t_i18n),
        variant: "error",
      });
    }
  }, [file.file_id, file.filename, toast, t_i18n]);
  return (
    <li className="flex items-center gap-2 rounded-sm border border-elevation-subtle px-2 py-1.5" data-file-id={file.file_id}>
      <TypeIcon className={cn("h-4 w-4 shrink-0", color)} aria-hidden />
      <div className="min-w-0 flex-1">
        <TruncatedText text={file.filename} className="text-default-primary" />
        <p className="content-caption text-default-secondary">{facts.join(" · ")}</p>
      </div>
      <Button
        variant="ghost"
        size="sm"
        className="gap-1"
        aria-label={t_i18n("Download {filename}", { filename: file.filename })}
        onClick={() => void download()}
      >
        <Icon name="download" size={16} aria-hidden /> {t_i18n("Download")}
      </Button>
    </li>
  );
}

/** The item's files, or nothing when it carries none. */
export function WorkItemFiles({ attachments }: { attachments: WorkItemAttachments | undefined }) {
  const { t_i18n } = useFormatter();
  const files = attachments?.files ?? [];
  const gone = attachments?.gone ?? 0;
  if (!files.length && !gone) return null;
  return (
    <div className="space-y-1.5" data-testid="work-item-files">
      {files.length > 0 && (
        <ul className="space-y-1.5">
          {files.map((file) => (
            <FileRow key={file.file_id} file={file} />
          ))}
        </ul>
      )}
      {gone > 0 && (
        <p className="content-caption text-default-secondary" data-testid="work-item-files-gone">
          {t_i18n("{count, plural, one {# attached file is no longer stored.} other {# attached files are no longer stored.}}", {
            count: gone,
          })}
        </p>
      )}
    </div>
  );
}
