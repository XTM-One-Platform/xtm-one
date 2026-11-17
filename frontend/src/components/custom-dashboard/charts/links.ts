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
 * The link a data source gives an item (a row's `_url`, an item's, an event's,
 * a node's or an action's `link`), as a widget opens it: a web address
 * (http or https, no credentials), or a page of this platform - what data
 * written in a dashboard may name (`check_internal_path`) - with the
 * platform's base path put in. Anything else is no link.
 */
import { withBasePath } from "@/lib/base-path";
import { safeExternalUrl } from "@/lib/utils";

import { isSafeDashboardHref } from "../spec";

export function dataHref(url: unknown): string | undefined {
  if (typeof url !== "string" || !url) return undefined;
  return isSafeDashboardHref(url) ? withBasePath(url) : safeExternalUrl(url);
}
