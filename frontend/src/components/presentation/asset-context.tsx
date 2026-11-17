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
 * How a deck turns a platform file id into an `<img src>`.
 *
 * On screen, platform files need the user's JWT in a header, so the bytes are
 * fetched and handed to the browser as a blob URL (`useAuthenticatedSrc`). The
 * static render page behind the PDF / PPTX export runs in a headless browser
 * that holds no session: it is handed a render token instead, and every asset
 * is reachable at a token-gated URL the browser can load directly. Both cases
 * go through this hook, so the block components never know which they are in.
 *
 * The deck response also carries the stored GEOMETRY of the logos it draws
 * (`asset_meta`: the trim box of each file's non-transparent pixels and
 * its ink), provided here by every slide root so a logo is cropped to its mark
 * and sized by it before its bytes arrive - the same reading on screen, in the
 * PDF and in the DOM the editable PPTX is measured from.
 */
import { createContext, useContext } from "react";

import { useAuthenticatedSrcState } from "@/hooks/useAuthenticatedSrc";
import type { DeckAssetMeta, DeckPictureLabel } from "@/types";

import { fileUrl } from "./blocks/helpers";

/** Maps a file id to a URL the browser can load with no extra credentials. */
type DeckAssetResolver = (fileId: string) => string;

const DeckAssetContext = createContext<DeckAssetResolver | null>(null);

export const DeckAssetProvider = DeckAssetContext.Provider;

/**
 * `src` for a platform file referenced by a deck, or `null` while it loads (and
 * on failure — callers render nothing rather than a broken image).
 */
export function useDeckAssetSrc(fileId: string | null | undefined): string | null {
  return useDeckAsset(fileId).src;
}

/**
 * `useDeckAssetSrc`, plus whether the bytes are still on their way: `loading`
 * while the on-screen authenticated fetch runs, never once it answered - a
 * failed fetch is not loading, so a deleted file never holds a ready signal.
 * A direct URL (the static render) is never loading here: its
 * `<img>` load is the caller's to track.
 *
 * `revision` (the stored geometry's `rev`) versions the on-screen URL: a file
 * replaced in place keeps its id, so without it an open deck would keep the
 * image it fetched and crop it with the replacement's trim box.
 */
export function useDeckAsset(
  fileId: string | null | undefined,
  revision?: string,
): { src: string | null; loading: boolean } {
  const resolver = useContext(DeckAssetContext);
  const direct = resolver && fileId ? resolver(fileId) : null;
  const url = fileUrl(fileId);
  const versioned = url && revision ? `${url}?v=${encodeURIComponent(revision)}` : url;
  // Always called (hooks rule); a no-op when the resolver already answered.
  const authenticated = useAuthenticatedSrcState(direct ? null : versioned);
  return direct ? { src: direct, loading: false } : { src: authenticated.src, loading: authenticated.status === "loading" };
}

const DeckAssetMetaContext = createContext<Record<string, DeckAssetMeta> | undefined>(undefined);

/** Provided by the slide roots from `presentation.asset_meta`. */
export const DeckAssetMetaProvider = DeckAssetMetaContext.Provider;

/** The stored geometry of a logo file, or `undefined` (drawn from the file's own size). */
export function useDeckAssetMeta(fileId: string | null | undefined): DeckAssetMeta | undefined {
  const meta = useContext(DeckAssetMetaContext);
  return fileId ? meta?.[fileId] : undefined;
}

/** The stored geometry of every logo the deck draws, for a figure that lays several out at once. */
export function useDeckAssetMetaMap(): Record<string, DeckAssetMeta> | undefined {
  return useContext(DeckAssetMetaContext);
}

/** A deck response with no `picture_labels` (an older one): one stable empty map. */
export const NO_PICTURE_LABELS: Record<string, DeckPictureLabel> = {};

/**
 * What the deck response says each quote picture file is (`picture_labels`),
 * provided by the slide hosts (`PresentationDeck`, `StaticSlide`) so
 * the viewer, a page document and the export page announce the same alt text.
 */
const DeckPictureLabelContext = createContext<Record<string, DeckPictureLabel>>(NO_PICTURE_LABELS);

export const DeckPictureLabelProvider = DeckPictureLabelContext.Provider;

/** The label of a picture file, or `null` when the deck response carries none for it. */
export function useDeckPictureLabel(fileId: string | null | undefined): DeckPictureLabel | null {
  const labels = useContext(DeckPictureLabelContext);
  return fileId ? (labels[fileId] ?? labels[fileId.toLowerCase()] ?? null) : null;
}
