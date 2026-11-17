// SPDX-License-Identifier: Apache-2.0

/**
 * What another app shared to this one (Android share sheet, the manifest's
 * `share_target`), as the service worker kept it (`public/sw.js`, same
 * database and store). Read once and deleted: shared content waits nowhere
 * longer than it takes the Share page and the chat to pick it up.
 */
const SHARE_DB = "xtm-one-share";
const SHARE_STORE = "pending";
/** Older than this, a share is stale (the person moved on): dropped unread. */
const SHARE_MAX_AGE_MS = 10 * 60 * 1000;

export interface SharedContent {
  text: string;
  files: File[];
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(SHARE_DB, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(SHARE_STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

/** Take the pending share, if any (it is deleted as it is read). */
export async function takeSharedContent(): Promise<SharedContent | null> {
  if (typeof indexedDB === "undefined") return null;
  try {
    const db = await openDb();
    const record = await new Promise<unknown>((resolve, reject) => {
      const tx = db.transaction(SHARE_STORE, "readwrite");
      const store = tx.objectStore(SHARE_STORE);
      const get = store.get("latest");
      get.onsuccess = () => {
        store.delete("latest");
        resolve(get.result);
      };
      tx.onerror = () => reject(tx.error);
    });
    db.close();
    if (!record || typeof record !== "object") return null;
    const { text, files, at } = record as { text?: unknown; files?: unknown; at?: unknown };
    if (typeof at !== "number" || Date.now() - at > SHARE_MAX_AGE_MS) return null;
    return {
      text: typeof text === "string" ? text : "",
      files: Array.isArray(files) ? files.filter((f): f is File => f instanceof File) : [],
    };
  } catch {
    return null;
  }
}

/** Files handed from the Share page to the chat within this page's life. */
let handedFiles: File[] = [];

export function handFilesToChat(files: File[]): void {
  handedFiles = files;
}

/** The chat takes them once, into its attachment tray. */
export function takeHandedFiles(): File[] {
  const files = handedFiles;
  handedFiles = [];
  return files;
}
