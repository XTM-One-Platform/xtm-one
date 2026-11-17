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
 * A video opened by its public link: the page the link opens, read
 * with no session like a public deck (`public-deck.ts`).
 */

/** The web app page a public link opens (`PUBLIC_LINK_ROUTE` in `backend/app/services/video_studio/public_links.py`). */
export const PUBLIC_VIDEO_ROUTE = "/public/videos";

/**
 * Whether *pathname* (without the base path) is a public video page: a dead
 * session stored in the browser must not send its visitor to the sign-in page
 * (`api.ts`).
 */
export function isPublicVideoPath(pathname: string): boolean {
  return pathname.startsWith(`${PUBLIC_VIDEO_ROUTE}/`);
}
