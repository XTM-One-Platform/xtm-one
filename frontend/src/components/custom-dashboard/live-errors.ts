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
 * One sentence per code a data source answers instead of data
 * (`dashboard_live.ERROR_CODES`, `dashboard_sources.ERROR_CODES`), `{platform}`
 * naming where it was read.
 */
import type { WidgetDataErrorCode } from "@/types";

/** One sentence per error code (`dashboard_live.ERROR_CODES`). @i18n-keys */
export const ERROR_MESSAGES = {
  integration_unavailable: "The {platform} integration of this widget is not available to you.",
  access_denied: "{platform} refused your access to this data.",
  timeout: "{platform} took too long to answer.",
  upstream_error: "{platform} could not answer.",
  unsupported: "This widget is drawn in {platform}.",
  rate_limited: "Too many reads at once; try again in a moment.",
  tenant_required: "Your {platform} account reaches several tenants: set the integration's Tenant field.",
  no_result: "No completed run of this assignment is visible to you yet.",
  not_run: "Ask the agent to see its answer.",
  running: "The agent is answering…",
  invalid_result: "The answer does not fit this visualization.",
  waiting: "Waiting for input.",
} as const satisfies Record<WidgetDataErrorCode, string>;
