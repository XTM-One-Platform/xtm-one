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

import { Icon } from "@filigran/design-system";
import { useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useCreateKnowledgeBase, useUploadDocument } from "@/hooks/api/useKnowledgeBaseApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { formatCompactNumber } from "@/lib/utils";
import { useFormatter } from "@/i18n";

/**
 * Quick-setup dialog for a knowledge base, launched from the wizard's
 * knowledge step — name, description, tags and the first documents, without
 * leaving the wizard (leaving would drop the whole client-side draft).
 *
 * As soon as the KB is created it is handed back to the caller (which selects
 * it). The create hook has already written it into the node catalog the
 * picker and the canvas read from, so it is listed and named at once, before
 * that catalog refetches. Documents are uploaded one by one
 * afterwards; indexing continues server-side, so the wizard never waits on it.
 */
export function NewKnowledgeBaseInline({
  onCreated,
  disabledReason,
}: {
  onCreated: (kb: { id: string; name: string }) => void;
  /** Why a base cannot be created now; the button is disabled while set. */
  disabledReason?: string;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const aiOff = useAiActionDisabled();
  const create = useCreateKnowledgeBase();
  const upload = useUploadDocument();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const fieldId = useId();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [files, setFiles] = useState<File[]>([]);
  const [uploadProgress, setUploadProgress] = useState<{ current: number; total: number } | null>(null);

  const busy = create.isPending || uploadProgress !== null;

  const reset = () => {
    setName("");
    setDescription("");
    setTags([]);
    setFiles([]);
    setUploadProgress(null);
  };

  const addFiles = (list: FileList | null) => {
    if (!list) return;
    setFiles((prev) => [...prev, ...Array.from(list)]);
  };

  const submit = async () => {
    if (!name.trim() || busy) return;
    create.mutate(
      {
        name: name.trim(),
        description: description.trim() || undefined,
        tags: tags.length > 0 ? tags : undefined,
      },
      {
        onSuccess: async (kb) => {
          // The KB exists as of now: hand it over at once instead of waiting
          // on the uploads below (slow, or failing, uploads must not delay it).
          onCreated({ id: kb.id, name: kb.name });
          let failed = 0;
          for (let i = 0; i < files.length; i++) {
            setUploadProgress({ current: i + 1, total: files.length });
            try {
              await upload.mutateAsync({ kbId: kb.id, file: files[i] });
            } catch {
              failed += 1;
            }
          }
          setUploadProgress(null);
          toast({
            title: t_i18n("Knowledge base created"),
            description: [
              files.length > 0
                ? t_i18n("{count, plural, one {# document uploaded} other {# documents uploaded}}", { count: files.length - failed })
                : null,
              failed > 0 ? t_i18n("{count, plural, one {# document failed to upload} other {# documents failed to upload}}", { count: failed }) : null,
            ].filter(Boolean).join(" "),
            variant: failed > 0 ? "info" : "success",
          });
          reset();
          setOpen(false);
        },
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!busy) { setOpen(o); if (!o) reset(); } }}>
      <Tip content={disabledReason} wrap={!!disabledReason}>
        <DialogTrigger asChild>
          <Button variant="ghost" size="sm" className="gap-1.5 text-default-secondary" disabled={!!disabledReason}>
            <Icon name="plus" size={16} /> {t_i18n("Create a new knowledge base")}
          </Button>
        </DialogTrigger>
      </Tip>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t_i18n("Create a new knowledge base")}</DialogTitle>
          <DialogDescription>
            {t_i18n("Name it, describe it, and drop in its first documents: everything else can be refined later in the Knowledge section.")}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor={`${fieldId}-name`}>{t_i18n("Name")}</Label>
            <Input id={`${fieldId}-name`} aria-label={t_i18n("Name")} value={name} onChange={(e) => setName(e.target.value)} autoFocus disabled={busy} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor={`${fieldId}-description`}>{t_i18n("Description (optional)")}</Label>
            <Input id={`${fieldId}-description`} aria-label={t_i18n("Description (optional)")} value={description} onChange={(e) => setDescription(e.target.value)} disabled={busy} />
          </div>
          <div className="space-y-1.5">
            <Label id={`${fieldId}-tags-label`}>{t_i18n("Tags")}</Label>
            <TagInput aria-labelledby={`${fieldId}-tags-label`} value={tags} onChange={setTags} placeholder="Add tags…" />
          </div>
          <div className="flex flex-col gap-1.5" role="group" aria-labelledby={`${fieldId}-documents-label`}>
            <Label id={`${fieldId}-documents-label`}>{t_i18n("Documents (optional)")}</Label>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }}
            />
            {files.length > 0 && (
              <ul className="space-y-1">
                {files.map((file, i) => (
                  <li key={`${file.name}-${i}`} className="flex items-center gap-2 rounded-sm bg-elevation-hover px-2 py-1.5 content-compact">
                    <Icon name="file-text" size={16} className="shrink-0 text-default-secondary" />
                    <span className="min-w-0 flex-1 truncate">{file.name}</span>
                    <span className="shrink-0 content-caption text-default-secondary">{formatCompactNumber(file.size)}B</span>
                    <button
                      type="button"
                      aria-label={t_i18n("Remove")}
                      className="inline-flex shrink-0 items-center justify-center text-default-secondary hover:text-default-primary"
                      disabled={busy}
                      onClick={() => setFiles((prev) => prev.filter((_, j) => j !== i))}
                    >
                      <Icon name="x" size={16} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <Button variant="outline" size="sm" className="gap-1.5 self-start" disabled={busy} onClick={() => fileInputRef.current?.click()}>
              <Icon name="upload" size={16} /> {t_i18n("Add documents")}
            </Button>
          </div>
        </div>

        <div className="flex items-center justify-end gap-3">
          {uploadProgress && (
            <span className="content-compact text-default-secondary">
              {t_i18n("Uploading {current} of {total}…", uploadProgress)}
            </span>
          )}
          <Button variant="ghost" disabled={busy} onClick={() => { setOpen(false); reset(); }}>{t_i18n("Cancel")}</Button>
          <Button disabled={!name.trim() || busy || aiOff} onClick={submit}>
            {busy ? t_i18n("Creating...") : t_i18n("Create")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
