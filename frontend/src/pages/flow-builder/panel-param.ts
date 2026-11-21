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
 * The flow builder's panel deep link.
 *
 * `?panel=owner-access` opens the Owner and access drawer - for an admin; the
 * param is consumed either way, like `?run=`. It is a URL contract on purpose:
 * the paused-flow notice links here, so the two sides need nothing of
 * each other but this string.
 */

export const FLOW_PANEL_PARAM = "panel";
const OWNER_ACCESS_PANEL = "owner-access";

/** Root-relative builder URL that opens the Owner and access drawer. */
export function ownerAccessUrl(flowId: string): string {
  return `/flows/library/${encodeURIComponent(flowId)}?${FLOW_PANEL_PARAM}=${OWNER_ACCESS_PANEL}`;
}

/** Whether the search params ask for the Owner and access drawer. */
export function wantsOwnerAccessPanel(params: URLSearchParams): boolean {
  return params.get(FLOW_PANEL_PARAM) === OWNER_ACCESS_PANEL;
}
