// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState } from "react";

import { getToken } from "@/lib/api";
import { withBasePath } from "@/lib/base-path";

/**
 * Fetch an authenticated URL and expose it as an object URL usable in `src`.
 *
 * Platform file/image endpoints require the JWT in an `Authorization` header,
 * which `<img src>` cannot send — so the bytes are fetched here and handed to
 * the browser as a blob URL, revoked on unmount or when the URL changes.
 *
 * Shared by the chat attachment previews and the presentation deck renderer;
 * never re-implement the fetch-then-createObjectURL dance locally.
 *
 * Callers pass the platform path root-relative (`/api/v1/chat/files/...`, as
 * the backend and the tools write it); the URL path prefix the platform is
 * served under is added here, at the sink.
 *
 * The download is ABORTED when the URL changes or the caller unmounts:
 * a conversation switched away from must not keep pulling and
 * decoding its pictures while the next one loads. The abort surfaces as a
 * rejected fetch, which the `cancelled` flag turns into a no-op.
 *
 * Returns `null` while loading and on any failure (caller renders a fallback).
 */
export function useAuthenticatedSrc(url: string | null | undefined): string | null {
  return useAuthenticatedSrcState(url).src;
}

/**
 * Where the fetch of the CURRENT url stands: `idle` without a url, `loading`
 * until its answer arrives, then `ready` or `failed`. What a caller that holds
 * a ready signal needs: a logo still on its way must hold the deck's
 * print, a logo whose file is gone must not hold it forever.
 */
type AuthenticatedSrcStatus = "idle" | "loading" | "ready" | "failed";

/** `useAuthenticatedSrc`, plus the status of the fetch behind it. */
export function useAuthenticatedSrcState(url: string | null | undefined): {
  src: string | null;
  status: AuthenticatedSrcStatus;
} {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  // The url whose answer has arrived, and how: derived against the current url,
  // so a new url reads `loading` from its first render, before its effect runs.
  const [answer, setAnswer] = useState<{ url: string; failed: boolean } | null>(null);
  const blobRef = useRef<string | null>(null);

  useEffect(() => {
    if (!url) {
      setBlobUrl(null);
      return;
    }
    let cancelled = false;
    const controller = new AbortController();
    const token = getToken();
    const headers: Record<string, string> = {};
    if (token) headers["Authorization"] = `Bearer ${token}`;

    fetch(withBasePath(url), { headers, signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.blob();
      })
      .then((blob) => {
        if (!cancelled) {
          const objectUrl = URL.createObjectURL(blob);
          blobRef.current = objectUrl;
          setBlobUrl(objectUrl);
          setAnswer({ url, failed: false });
        }
      })
      .catch(() => {
        if (!cancelled) {
          setBlobUrl(null);
          setAnswer({ url, failed: true });
        }
      });

    return () => {
      cancelled = true;
      controller.abort();
      if (blobRef.current) {
        URL.revokeObjectURL(blobRef.current);
        blobRef.current = null;
      }
    };
  }, [url]);

  const status: AuthenticatedSrcStatus = !url
    ? "idle"
    : answer?.url !== url
      ? "loading"
      : answer.failed
        ? "failed"
        : "ready";
  return { src: blobUrl, status };
}

/**
 * `useAuthenticatedSrc` over several URLs tried in order: the first that
 * loads is shown, a failed one hands over to the next (a template tile's
 * thumbnail, then the original it drew before). `null` while loading
 * and once every URL failed.
 */
export function useFirstAuthenticatedSrc(candidates: readonly (string | null | undefined)[]): string | null {
  const urls = candidates.filter((url): url is string => !!url);
  const key = urls.join("\n");
  const [skipped, setSkipped] = useState<{ key: string; count: number }>({ key, count: 0 });
  const index = skipped.key === key ? skipped.count : 0;
  const { src, status } = useAuthenticatedSrcState(urls[index] ?? null);
  if (status === "failed" && index < urls.length - 1) {
    setSkipped({ key, count: index + 1 });
  }
  return status === "ready" ? src : null;
}
