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

import { useState, useCallback, useRef } from "react";
import { FileText, Trash2, Upload, Plus, File, RefreshCw, Download, Lock } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge, BadgeFocusTrigger } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import { CompanyManagedBanner } from "@/components/ui/company-managed-banner";
import { SharedBanner } from "@/components/ui/shared-banner";
import { Tip } from "@/components/ui/tooltip";
import { useAuth } from "@/hooks/useAuth";
import { Pagination } from "@/components/ui/pagination";
import { useToast } from "@/components/ui/toast";
import {
  useKnowledgeDocuments, useKnowledgeDocumentCount, useAddTextDocument, useUploadDocument, useDeleteDocument, useReprocessDocuments, downloadDocument,
} from "@/hooks/api/useKnowledgeBaseApi";
import { cn, formatCompactNumber, pluralize, isEntityShared } from "@/lib/utils";
import { safeHttpUrl } from "@/lib/safe-url";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import type { KnowledgeBase, KnowledgeDocument } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";

const DOCS_PAGE_SIZE = 50;

export function DocumentsTab({ kb, canManage }: { kb: KnowledgeBase; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const { user } = useAuth();
  const isShared = isEntityShared(kb, user?.id);
  const [page, setPage] = useState(0);
  const { data: docs } = useKnowledgeDocuments(kb.id, DOCS_PAGE_SIZE, page * DOCS_PAGE_SIZE);
  const { data: countData } = useKnowledgeDocumentCount(kb.id);
  const total = countData?.count ?? kb.document_count ?? 0;
  const addText = useAddTextDocument();
  const aiOff = useAiActionDisabled();
  const uploadDoc = useUploadDocument();
  const removeDoc = useDeleteDocument();
  const reprocessDocs = useReprocessDocuments();
  const { toast } = useToast();
  const [adding, setAdding] = useState(false);
  const [filename, setFilename] = useState("");
  const [content, setContent] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const stuckCount = docs?.filter((d) => d.status === "pending" || d.status === "processing" || d.status === "error").length || 0;

  const handleAdd = () => {
    if (!filename.trim() || !content.trim()) return;
    addText.mutate(
      { kbId: kb.id, filename, content },
      { onSuccess: () => { toast({ title: t_i18n("Document added"), variant: "success" }); setAdding(false); setFilename(""); setContent(""); } },
    );
  };

  const handleFileUpload = useCallback((file: globalThis.File) => {
    const BINARY_EXTS = ["pdf", "docx", "doc", "xls", "xlsx", "pptx"];
    const ext = file.name.split(".").pop()?.toLowerCase() || "";
    if (BINARY_EXTS.includes(ext)) {
      uploadDoc.mutate({ kbId: kb.id, file }, { onSuccess: () => toast({ title: t_i18n("{name} uploaded", { name: file.name }), variant: "success" }) });
    } else {
      const reader = new FileReader();
      reader.onload = (e) => {
        const text = e.target?.result as string;
        if (text) addText.mutate({ kbId: kb.id, filename: file.name, content: text, source_type: file.name.endsWith(".md") ? "markdown" : "text" }, { onSuccess: () => toast({ title: t_i18n("{name} added", { name: file.name }), variant: "success" }) });
      };
      reader.readAsText(file);
    }
  }, [kb.id, addText, uploadDoc, toast, t_i18n]);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    const files = Array.from(e.dataTransfer.files);
    files.forEach(handleFileUpload);
    if (files.length > 0) setUploadOpen(false);
  }, [handleFileUpload]);

  const handleFileInput = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    files.forEach(handleFileUpload);
    if (fileInputRef.current) fileInputRef.current.value = "";
    if (files.length > 0) setUploadOpen(false);
  }, [handleFileUpload]);

  // Content-mutation gate for this tab: the upload / paste / delete controls.
  // ``kb.is_read_only`` is the explicit ground-truth lock; the others are the
  // pre-existing platform-managed and permission cases (a package-installed
  // KB arrives here with `canManage` false). Reprocess stays available except
  // on a package-installed KB, whose reprocess route refuses it too.
  const isReadOnly = kb.is_system_managed || kb.is_builtin || kb.is_read_only || !canManage;

  return (
    <div className="space-y-4">
      {kb.is_system_managed && (
        <div className="rounded-sm bg-accent/5 p-3">
          <p className="content-compact text-accent">{t_i18n("This knowledge base is system-managed. Documents are added and updated automatically by assignment executions.")}</p>
        </div>
      )}

      {kb.is_builtin && !kb.is_system_managed && (
        <div className="rounded-sm bg-accent/5 p-3 flex items-start gap-2.5">
          <FileText className="h-3.5 w-3.5 text-accent shrink-0 mt-0.5" />
          <p className="content-compact text-accent/90 leading-relaxed">
            {t_i18n("Documents in this built-in knowledge base are read-only. Use Import Sources to manage content.")}
          </p>
        </div>
      )}

      {kb.is_read_only && !kb.is_builtin && !kb.is_system_managed && (
        <div className="rounded-sm bg-feedback-alert-primary/5 p-3 flex items-start gap-2.5">
          <Lock className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0 mt-0.5" />
          <p className="content-compact text-feedback-warning-primary leading-relaxed">
            {t_i18n("This knowledge base is read-only: it is maintained as ground truth. Documents cannot be added, edited or deleted, and agents cannot write to it. Turn off read-only in Configuration to make changes.")}
          </p>
        </div>
      )}

      {kb.is_package_origin && !kb.is_builtin && (
        <CompanyManagedBanner entityLabel="knowledge base" isPackageOrigin />
      )}
      {!kb.is_builtin && !kb.is_package_origin && kb.is_company_managed && !canManage && (
        <CompanyManagedBanner entityLabel="knowledge base" />
      )}
      {isShared && <SharedBanner entityLabel="knowledge base" ownerName={kb.owner_name} />}

      <div className="flex items-center justify-between">
        <p className="content-base text-default-secondary">
          {formatCompactNumber(total)} {t_i18n(pluralize(total, "document"))}
        </p>
        <div className="flex gap-2">
          {/* The reprocess route refuses a package-installed KB too. */}
          {stuckCount > 0 && !kb.is_package_origin && (
            <Button variant="outline" size="sm" className="gap-1.5" disabled={reprocessDocs.isPending || aiOff}
              onClick={() =>
                reprocessDocs.mutate(
                  { kbId: kb.id },
                  { onSuccess: (data) => toast({ title: data.message, variant: "success" }) }
                )
              }>
              <RefreshCw className={cn("h-3.5 w-3.5", reprocessDocs.isPending && "animate-spin")} />
              {reprocessDocs.isPending ? t_i18n("Reprocessing...") : t_i18n("Reprocess ({count})", { count: stuckCount })}
            </Button>
          )}
          {!isReadOnly && (
            <>
              <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => setUploadOpen(true)}>
                <Upload className="h-3.5 w-3.5" /> {t_i18n("Upload Files")}
              </Button>
              <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => setAdding(true)}>
                <Plus className="h-3.5 w-3.5" /> {t_i18n("Paste Text")}
              </Button>
            </>
          )}
        </div>
      </div>

      {/* Hidden file input */}
      <input ref={fileInputRef} type="file" multiple accept=".txt,.md,.csv,.json,.log,.pdf,.docx,.doc,.xls,.xlsx,.html,.xml,.py,.js,.ts,.yaml,.yml" className="hidden" onChange={handleFileInput} />

      {/* Upload dialog */}
      <Dialog open={uploadOpen} onOpenChange={setUploadOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t_i18n("Upload Files")}</DialogTitle>
            <DialogDescription>
              {t_i18n("Drag & drop files or click to browse. Supports PDF, Word, Excel, Markdown, code, and more.")}
            </DialogDescription>
          </DialogHeader>
          <div
            role="button"
            tabIndex={0}
            className={cn(
              "rounded-sm border-2 border-dashed p-10 text-center transition-colors cursor-pointer",
              isDragging ? "border-accent bg-accent/5" : "border-elevation-subtle-soft hover:border-accent/40 hover:bg-elevation-hover",
            )}
            onDragOver={(e) => { e.preventDefault(); setIsDragging(true); }}
            onDragLeave={() => setIsDragging(false)}
            onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
            onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") fileInputRef.current?.click(); }}
          >
            <File className={cn("h-10 w-10 mx-auto mb-3 transition-colors", isDragging ? "text-accent" : "text-default-secondary")} />
            <p className="content-base font-medium text-default-secondary mb-1">
              {isDragging ? t_i18n("Drop files here...") : t_i18n("Click to browse or drag & drop")}
            </p>
            <p className="content-compact text-default-secondary">
              {t_i18n("PDF, Word, Excel, Markdown, JSON, CSV, code files, and more")}
            </p>
          </div>
        </DialogContent>
      </Dialog>

      {/* Paste text dialog */}
      <Dialog open={adding} onOpenChange={(open) => { setAdding(open); if (!open) { setFilename(""); setContent(""); } }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t_i18n("Paste Text")}</DialogTitle>
            <DialogDescription>
              {t_i18n("Add a document by pasting markdown or plain text content.")}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <Input aria-label={t_i18n("Filename (e.g. opencti-docs.md)")} placeholder={t_i18n("Filename (e.g. opencti-docs.md)")} value={filename} onChange={(e) => setFilename(e.target.value)} />
            <textarea
              className={`flex min-h-[180px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
              placeholder={t_i18n("Paste markdown or text...")}
              value={content}
              onChange={(e) => setContent(e.target.value)}
            />
            <div className="flex gap-2 justify-end">
              <Button variant="ghost" size="sm" onClick={() => setAdding(false)}>{t_i18n("Cancel")}</Button>
              <AiDisabledTip>
              <Button size="sm" onClick={handleAdd} disabled={addText.isPending || !filename.trim() || !content.trim() || aiOff}>
                {addText.isPending ? t_i18n("Adding...") : t_i18n("Add Document")}
              </Button>
              </AiDisabledTip>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* Document list: one pager, in the footer (#10215 criterion 6). */}
      <div className="space-y-2">
        {docs && docs.length > 0 ? (
          docs.map((doc) => (
            <Card key={doc.id}>
              <CardContent className="flex items-center justify-between p-3">
                <div className="flex items-center gap-3 min-w-0">
                  <FileText className="h-4 w-4 shrink-0 text-default-secondary" />
                  <div className="min-w-0">
                    <p className="content-compact font-medium truncate">{doc.filename}</p>
                    <div className="flex items-center gap-2 mt-0.5">
                      {doc.status === "error" && doc.error_message ? (
                        <Tip content={doc.error_message}>
                          <BadgeFocusTrigger className="cursor-help">
                            <Badge variant="destructive" className="text-[8px]">{doc.status}</Badge>
                          </BadgeFocusTrigger>
                        </Tip>
                      ) : (
                        <Badge variant={doc.status === "ready" ? "success" : doc.status === "error" ? "destructive" : "outline"} className="text-[8px]">{doc.status}</Badge>
                      )}
                      <FigureBadge doc={doc} />
                      {doc.chunk_count > 0 && <span className="text-[9px] text-default-secondary">{t_i18n("{count} chunks", { count: formatCompactNumber(doc.chunk_count) })}</span>}
                      <span className="text-[9px] text-default-secondary">{doc.source_type}</span>
                      <DocumentSourceLink url={doc.source_url} />
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  {(doc.status === "ready" || doc.status === "error") && (
                    <Tip content={doc.status === "error" ? t_i18n("Download stored content to troubleshoot") : t_i18n("Download source file")}>
                      <Button aria-label={doc.status === "error" ? t_i18n("Download stored content to troubleshoot") : t_i18n("Download source file")} variant="ghost" size="sm" className="h-7 w-7 p-0 text-default-secondary hover:text-default-primary" onClick={() => downloadDocument(kb.id, doc.id, doc.filename).catch(() => toast({ title: t_i18n("Download failed"), variant: "error" }))}><Download className="h-3.5 w-3.5" /></Button>
                    </Tip>
                  )}
                  {!isReadOnly && (
                    <Button aria-label={t_i18n("Delete document")} variant="ghost" size="sm" className="h-7 w-7 p-0 text-feedback-error-primary hover:text-feedback-error-primary" disabled={aiOff} onClick={() => removeDoc.mutate({ kbId: kb.id, docId: doc.id }, { onSuccess: () => toast({ title: t_i18n("Removed"), variant: "success" }) })}><Trash2 className="h-3.5 w-3.5" /></Button>
                  )}
                </div>
              </CardContent>
            </Card>
          ))
        ) : (
          <Card className="border-dashed"><CardContent className="py-8 text-center"><FileText className="h-8 w-8 text-default-disabled mx-auto mb-2" /><p className="content-base text-default-secondary">{kb.is_system_managed ? t_i18n("No documents yet. Assignment executions will populate this knowledge base.") : t_i18n("No documents yet. Upload files, paste text, or sync from a source.")}</p></CardContent></Card>
        )}

        <Pagination page={page} pageSize={DOCS_PAGE_SIZE} total={total} onChange={setPage} />
      </div>
    </div>
  );
}

/** The figure pass of a PDF: shown only when its figures were described, or
 *  could not all be. A shortfall is a warning, never a plain "ready". */
function FigureBadge({ doc }: { doc: KnowledgeDocument }) {
  const { t_i18n } = useFormatter();
  const status = doc.figure_status;
  if (!status) return null;
  if (status === "complete") {
    if (!doc.figure_pages) return null;
    return <span className="content-caption text-default-secondary">{t_i18n("{count, plural, one {# page with figures described} other {# pages with figures described}}", { count: doc.figure_pages })}</span>;
  }
  if (status === "pending") {
    return <Badge variant="outline" className="content-caption">{t_i18n("Figures pending")}</Badge>;
  }
  const label = status === "partial" ? t_i18n("Figures partly described") : t_i18n("Figures not described");
  const badge = <Badge variant="warning" className="content-caption">{label}</Badge>;
  if (!doc.figure_note) return badge;
  return (
    <Tip content={doc.figure_note}>
      <BadgeFocusTrigger className="cursor-help">{badge}</BadgeFocusTrigger>
    </Tip>
  );
}

/** A document's source page, linked only when it is a web address (`safeHttpUrl`). */
function DocumentSourceLink({ url }: { url: string | null | undefined }) {
  const { t_i18n } = useFormatter();
  const href = safeHttpUrl(url);
  if (!href) return null;
  return <a href={href} target="_blank" rel="noopener noreferrer" className="text-[9px] text-accent hover:underline">{t_i18n("source")}</a>;
}
