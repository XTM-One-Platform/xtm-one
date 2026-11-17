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
 * How a file of an agent reply shows as a card, and how two files of the same
 * name tell apart.
 *
 * A file's tag decides its card: the download card of a deliverable, or the
 * compact chip of a working file (`FileChips.tsx`). Across the conversation,
 * files sharing a name and type are numbered (v1, v2...) so two cards that
 * would otherwise read the same never do.
 *
 * Computed at render time from what every message already carries, so stored
 * messages need no migration and their markers keep resolving.
 *
 * Pure functions, no React and no `@/` import (like `attachments.ts`), so
 * `file-revisions.test.ts` runs under `node --test`.
 */
import type { ChatMessageAttachment } from "../../types/index.ts";

type FileLike = Pick<ChatMessageAttachment, "filename" | "file_id" | "content_type" | "type"> & {
  size?: number | null;
  created_at?: string | null;
  sha256?: string | null;
  file_tag?: ChatMessageAttachment["file_tag"];
};

/** How many hex characters of the SHA-256 a card's tooltip shows. */
const SHA_PREFIX_LENGTH = 12;

export type FileTag = "working_file" | "download_file";

/** `working_file` renders the compact chip; anything else the download card. */
export function fileTag(att: Pick<FileLike, "file_tag">): FileTag {
  return att.file_tag === "working_file" ? "working_file" : "download_file";
}

function fileKey(att: Pick<FileLike, "file_id">): string | null {
  return att.file_id ? att.file_id.toLowerCase() : null;
}

/**
 * What makes two files revisions of one another: the same name and the same
 * kind of file (the MIME type without its parameters, else the type label).
 * The rule the backend's same-name note applies (`FileContext.produced_same_name`).
 */
export function revisionKey(att: Pick<FileLike, "filename" | "content_type" | "type">): string {
  const mime = (att.content_type ?? "").split(";")[0].trim().toLowerCase();
  const kind = mime || (att.type ?? "").trim().toLowerCase();
  return `${att.filename.trim()}\n${kind}`;
}

/**
 * A message's files oldest first: by their own creation time when every one of
 * them carries it, else in the order the message lists them.
 */
function inCreationOrder<T extends FileLike>(attachments: readonly T[]): T[] {
  const ordered = [...attachments];
  if (ordered.every((att) => !!att.created_at && !Number.isNaN(Date.parse(att.created_at)))) {
    ordered.sort((a, b) => Date.parse(a.created_at!) - Date.parse(b.created_at!));
  }
  return ordered;
}

export interface FileRevision {
  /** 1-based, oldest first. */
  revision: number;
  /** How many files of the conversation share the name and type. */
  count: number;
  /** When the file was stored: its own time, else its message's. */
  createdAt: string | null;
  /** The lower-cased file id of the newest file sharing the name and type (itself when it is the newest). */
  latestId: string;
}

/** The newest revision's file id when this file is an older one, else null: what "Go to the newer version" brings into view. */
export function newerRevisionId(revision: FileRevision | null | undefined): string | null {
  if (!revision || revision.revision >= revision.count) return null;
  return revision.latestId;
}

/**
 * Every file of the conversation by lower-cased file id, with its revision
 * among the files sharing its name and type - numbered in message order, then
 * in creation order within a message.
 */
export function conversationRevisions(
  messages: ReadonlyArray<{ created_at?: string | null; attachments?: readonly FileLike[] | null }>,
): Map<string, FileRevision> {
  const groups = new Map<string, Array<{ id: string; createdAt: string | null }>>();
  const seen = new Set<string>();
  for (const msg of messages) {
    for (const att of inCreationOrder(msg.attachments ?? [])) {
      const id = fileKey(att);
      if (!id || seen.has(id)) continue;
      seen.add(id);
      const key = revisionKey(att);
      const group = groups.get(key) ?? [];
      group.push({ id, createdAt: att.created_at || msg.created_at || null });
      groups.set(key, group);
    }
  }
  const revisions = new Map<string, FileRevision>();
  for (const group of groups.values()) {
    const latestId = group[group.length - 1].id;
    group.forEach(({ id, createdAt }, index) => {
      revisions.set(id, { revision: index + 1, count: group.length, createdAt, latestId });
    });
  }
  return revisions;
}

export interface FileCardFacts {
  /** When the file was stored, for the card's time. */
  createdAt: string | null;
  /** The revision badge (v1, v2...), only when the name is not unique in the conversation. */
  revision: number | null;
  /** The exact size in bytes, shown in place of the rounded one when the name is not unique. */
  exactBytes: number | null;
  fileId: string | null;
  shaPrefix: string | null;
}

/** What a file card says beyond its name, type and size. */
export function fileCardFacts(att: FileLike, revision?: FileRevision | null): FileCardFacts {
  const collides = !!revision && revision.count > 1;
  return {
    createdAt: revision?.createdAt ?? att.created_at ?? null,
    revision: collides ? revision.revision : null,
    exactBytes: collides && typeof att.size === "number" && Number.isFinite(att.size) ? att.size : null,
    fileId: att.file_id ?? null,
    shaPrefix: att.sha256 ? att.sha256.slice(0, SHA_PREFIX_LENGTH).toLowerCase() : null,
  };
}
