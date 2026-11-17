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
 * Where a tile that lists template assets reads a picture from.
 *
 * The thumbnail route renders the raster formats of its decoder set
 * (`THUMBNAIL_DECODERS` in `services/template_thumbnails.py`); every other
 * asset - an SVG, a TIFF, an icon, a HEIC photo - is drawn from its original
 * exactly as before the route existed, and so is a raster the route answers
 * it has no thumbnail for. A tile tries the sources in order and shows the
 * first that loads.
 */

/** The declared types whose bytes the thumbnail route decodes (aliases included). */
const TEMPLATE_THUMBNAIL_TYPES: ReadonlySet<string> = new Set([
  "image/png",
  "image/x-png",
  "image/apng",
  "image/jpeg",
  "image/jpg",
  "image/pjpeg",
  "image/gif",
  "image/webp",
  "image/bmp",
  "image/x-ms-bmp",
  "image/avif",
]);

/** Whether the thumbnail route renders an asset stored under *contentType*. */
export function hasTemplateThumbnail(contentType: string | null | undefined): boolean {
  const kind = (contentType ?? "").split(";", 1)[0].trim().toLowerCase();
  return TEMPLATE_THUMBNAIL_TYPES.has(kind);
}

/**
 * The URLs a tile tries, in order: the thumbnail when the route renders the
 * type, then the original the tile showed before (`null` entries are skipped).
 */
export function templateAssetPreviewSources(
  contentType: string | null | undefined,
  thumbnailUrl: string | null,
  originalUrl: string | null,
): string[] {
  const sources = hasTemplateThumbnail(contentType) ? [thumbnailUrl, originalUrl] : [originalUrl];
  return sources.filter((url): url is string => !!url);
}
