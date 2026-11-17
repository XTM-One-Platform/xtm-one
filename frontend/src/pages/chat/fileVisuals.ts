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
 * Shared presentation helpers for chat file attachments.
 *
 * One module so the Conversation files panel (``ConversationFilesPanel.tsx``)
 * renders a file exactly the way the inline chips in the transcript do — same icon, same colour, same type label, same
 * size formatting. Two independent copies of ``mimeVisual`` would drift, and a
 * file whose chip is a green spreadsheet in the transcript but a grey generic
 * file in the panel reads as two different files.
 *
 * A module of its own, not a component file's exports: a module that exports
 * both components and plain functions trips
 * ``react-refresh/only-export-components`` and breaks Vite's Fast Refresh.
 */

import {
  File,
  FileCode,
  FileSpreadsheet,
  FileText,
  Image as ImageIcon,
} from "lucide-react";
import type React from "react";
import { getToken } from "@/lib/api";
import { withBasePath } from "@/lib/base-path";

/** Minimal shape the visual helpers need — satisfied by chat attachments and conversation files alike. */
interface FileVisualLike {
  /** Short type token (usually a filename extension), when the caller has one. */
  type?: string | null;
  /** Full MIME type, possibly with parameters (e.g. ``text/plain; charset=utf-8``). */
  content_type?: string | null;
}

const MIME_BY_EXTENSION: Record<string, string> = {
  txt: "text/plain",
  md: "text/markdown",
  pdf: "application/pdf",
  doc: "application/msword",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  csv: "text/csv",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  xls: "application/vnd.ms-excel",
  py: "text/x-python",
  js: "text/javascript",
  ts: "text/typescript",
  tsx: "text/typescript",
  jsx: "text/javascript",
  json: "application/json",
  xml: "application/xml",
  html: "text/html",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  svg: "image/svg+xml",
};

/**
 * The short type a known MIME type goes by (``docx`` for the long Office
 * ``application/vnd.openxmlformats-...`` names): the first extension that maps
 * to it in ``MIME_BY_EXTENSION``.
 */
const EXTENSION_BY_MIME: Record<string, string> = Object.entries(MIME_BY_EXTENSION).reduce<Record<string, string>>(
  (acc, [extension, mime]) => {
    if (!(mime in acc)) acc[mime] = extension;
    return acc;
  },
  {},
);

/** Resolve a usable base MIME type from an attachment's metadata. */
export function resolveAttachmentMime(att: FileVisualLike): string {
  const raw = att.content_type?.trim().toLowerCase();
  if (raw) {
    // Strip any MIME parameters (e.g. "; charset=utf-8") so labels, icons,
    // and exact-match checks use the base type. Upload paths persist the
    // client-provided content_type verbatim, so a value like
    // "text/plain; charset=utf-8" reaches here and would otherwise render
    // as "PLAIN; CHARSET=UTF-8" in the chip label.
    const base = raw.split(";", 1)[0].trim();
    if (base) return base;
  }
  return MIME_BY_EXTENSION[att.type?.toLowerCase() ?? ""] ?? "application/octet-stream";
}

/** Pick the icon + colour for a MIME type. */
export function mimeVisual(mime: string): { icon: React.ElementType; color: string } {
  if (mime.startsWith("image/")) return { icon: ImageIcon, color: "text-filigran-ia-primary" };
  if (mime.startsWith("text/csv") || mime.includes("spreadsheet") || mime.includes("excel")) {
    return { icon: FileSpreadsheet, color: "text-feedback-success-primary" };
  }
  if (mime.includes("json") || mime.includes("xml") || mime.includes("javascript") || mime.includes("typescript")) {
    return { icon: FileCode, color: "text-feedback-warning-primary" };
  }
  if (mime.startsWith("text/")) return { icon: FileText, color: "text-feedback-info-primary" };
  if (mime === "application/pdf") return { icon: FileText, color: "text-feedback-error-primary" };
  return { icon: File, color: "text-default-secondary" };
}

/**
 * Short uppercase type label for a chip/row (e.g. ``PDF``, ``CSV``).
 *
 * A known MIME type gives its short type (``DOCX``, never
 * ``VND.OPENXMLFORMATS-OFFICEDOCUMENT...``, which overflowed the 300 px file
 * chip); an unknown one gives its subtype, which the chip truncates.
 */
export function mimeLabel(att: FileVisualLike, mime: string): string {
  if (att.type && att.type.trim()) return att.type.trim().toUpperCase();
  const known = EXTENSION_BY_MIME[mime.trim().toLowerCase()];
  if (known) return known.toUpperCase();
  const slashIndex = mime.indexOf("/");
  if (slashIndex >= 0 && slashIndex < mime.length - 1) {
    return mime.slice(slashIndex + 1).toUpperCase();
  }
  return "FILE";
}

/** Derive a type token from a filename when the API sends no ``type`` field. */
export function extensionFromFilename(filename: string): string | undefined {
  const dot = filename.lastIndexOf(".");
  // Reject a leading dot (dotfiles like ``.env`` have no extension) and a
  // trailing dot (``report.`` would otherwise yield an empty token).
  if (dot <= 0 || dot === filename.length - 1) return undefined;
  return filename.slice(dot + 1).toLowerCase();
}

/** Human-readable byte size; empty string for zero/unknown sizes. */
export function formatFileSize(bytes: number): string {
  if (!bytes || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Download a file through an authenticated fetch and hand it to the browser.
 *
 * Throws on transport / auth failure so callers can surface a toast.
 * Callers MUST either ``await`` and ``try / catch``, or use ``.catch(...)`` on
 * the returned promise — letting the rejection escape produces an
 * unhandled-rejection warning AND silently fails the download with no
 * user-visible feedback.
 */
export async function authenticatedDownload(url: string, filename: string) {
  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  // ``download_url`` is root-relative as the backend writes it; the URL path
  // prefix the platform is served under goes on at the sink.
  const res = await fetch(withBasePath(url), { headers });
  if (!res.ok) {
    // Try to extract a human-readable error from a JSON body
    // (FastAPI conventionally returns ``{"detail": "..."}`` on 4xx).
    // Fall back to the status code if the body isn't JSON.
    let detail: string | null = null;
    try {
      const ct = res.headers.get("content-type") ?? "";
      if (ct.includes("application/json")) {
        const body = await res.clone().json();
        if (body && typeof body === "object" && "detail" in body) {
          const raw = (body as { detail: unknown }).detail;
          if (typeof raw === "string") detail = raw;
        }
      }
    } catch {
      // ignore JSON parse errors — fall back to status code
    }
    // `status` travels on the error so the toast can say what happened in
    // the reader's language (lib/download-error.ts).
    throw Object.assign(
      new Error(detail ? `HTTP ${res.status}: ${detail}` : `HTTP ${res.status}`),
      { status: res.status },
    );
  }
  const blob = await res.blob();
  const blobUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = blobUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(blobUrl);
}
