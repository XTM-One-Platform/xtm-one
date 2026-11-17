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
 * A deck opened by its public link, as the deck components take it.
 *
 * `GET /public/presentations/{token}` answers what a renderer needs and
 * nothing about who owns, edits or may open the deck - a whitelist on the
 * server. The stage and its slides take a `Presentation`, so the fields the
 * public answer leaves out are filled the way a visitor holds them: no owner,
 * no grant, nothing to manage or comment on.
 */
import type { Presentation, PublicPresentation } from "@/types";

/** The web app page a public link opens (`PUBLIC_LINK_ROUTE` in `presentation_public_links.py`). */
export const PUBLIC_DECK_ROUTE = "/public/presentations";

/**
 * Whether *pathname* (without the base path) is a public deck page: read with
 * no session, so a dead session stored in the browser must not send its
 * visitor to the sign-in page (`api.ts`).
 */
export function isPublicDeckPath(pathname: string): boolean {
  return pathname.startsWith(`${PUBLIC_DECK_ROUTE}/`);
}

/** Where a public page loads one of the deck's pictures: only those the deck shows are served. */
export function publicAssetUrl(apiBase: string, token: string, fileId: string): string {
  return `${apiBase}/public/presentations/${encodeURIComponent(token)}/assets/${encodeURIComponent(fileId)}`;
}

export function publicDeckAsPresentation(deck: PublicPresentation): Presentation {
  return {
    ...deck,
    user_id: null,
    owner_name: null,
    is_company_managed: false,
    shared_group_ids: null,
    shared_group_permissions: null,
    shared_user_ids: null,
    shared_user_permissions: null,
    access_restricted: false,
    permission: null,
    can_manage: false,
    has_public_link: true,
    conversation_id: null,
    assignment_run_id: null,
    template_container_id: null,
    template_name: null,
    updated_by_user_id: null,
    updated_by_name: null,
    open_comment_count: 0,
    created_at: null,
  };
}
