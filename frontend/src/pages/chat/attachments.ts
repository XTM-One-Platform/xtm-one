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
 * A chat file as the page's file chips take it: the backend's label
 * for its type (`fileTypeLabel`, the spelling of `_attachment_type_label`),
 * and the stored files of a conversation by id (`conversationFileMap`), which
 * an answer's `[[FILE:…]]` markers are drawn from.
 *
 * Pure functions, no React and no `@/` import, so `attachments.test.ts` runs
 * under `node --test`.
 */
import type { ChatMessageAttachment } from "../../types/index.ts";

/**
 * The type label the backend stores on a chat attachment, spelled exactly as
 * `_attachment_type_label` in `backend/app/api/v1/chat.py`: the filename
 * extension, else the MIME subtype without its structured-syntax suffix
 * (`image/svg+xml` -> `svg`) and with a vendor subtype cut to its last
 * segment, else `file`. A pasted screenshot is `png`, never `image/png`.
 */
export function fileTypeLabel(filename: string, contentType: string | null | undefined): string {
  if (filename.includes(".")) {
    const ext = (filename.split(".").pop() ?? "").trim().toLowerCase();
    if (ext) return ext;
  }
  if (contentType && contentType.includes("/")) {
    let subtype = contentType.slice(contentType.indexOf("/") + 1).split(";")[0].trim().toLowerCase();
    subtype = subtype.split("+")[0].trim();
    if (subtype.startsWith("vnd.")) subtype = subtype.split(".").pop() ?? "";
    if (subtype && subtype !== "octet-stream") return subtype;
  }
  return "file";
}

/**
 * Every downloadable file the messages show, by lower-cased file id, first
 * message first. A reply's `[[FILE:id]]` marker may name a file an
 * EARLIER message shows - the user's upload, a file of a previous turn - and a
 * stored file is shown by at most one message, so its chip is that message's,
 * with that message's download URL.
 */
export function conversationFileMap(
  messages: ReadonlyArray<{ attachments?: ChatMessageAttachment[] | null }>,
): Map<string, ChatMessageAttachment> {
  const files = new Map<string, ChatMessageAttachment>();
  for (const msg of messages) {
    for (const att of msg.attachments ?? []) {
      const id = att.file_id?.toLowerCase();
      if (id && att.download_url && !files.has(id)) files.set(id, att);
    }
  }
  return files;
}

