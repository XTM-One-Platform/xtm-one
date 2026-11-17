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

import type { ProviderCatalogItem } from "@/types";

/**
 * Whether to offer the Request URL of Slack's events endpoint
 * (`/api/v1/integrations/notifications/slack`) for real-time message events.
 *
 * Every Slack integration of the platform shares that endpoint, so its URL is
 * offered only where the platform can prove who sent a delivery: with the
 * signing secret of the Slack app, which the catalog asks for as
 * `webhook_secret` (on the integration, or on the Slack OAuth default for the
 * administrator's app). Without that field there is nothing to verify a
 * delivery with, and new messages arrive through polling alone.
 */
export function offersSlackEventsUrl(
  catalogEntry: Pick<ProviderCatalogItem, "provider" | "extra_fields"> | null | undefined,
): boolean {
  return catalogEntry?.provider === "slack" && catalogEntry.extra_fields?.webhook_secret != null;
}

/**
 * The app-level webhook path Settings > OAuth defaults gives the administrator
 * for a provider, or `null` when there is none to give. The provider hook keys
 * a Slack message as a plain `message`, which never triggers "New Slack
 * message" assignments, so Slack gets the events endpoint instead, under the
 * rule of {@link offersSlackEventsUrl}.
 */
export function oauthDefaultWebhookPath(
  catalogEntry: Pick<ProviderCatalogItem, "provider" | "extra_fields">,
): string | null {
  if (catalogEntry.provider !== "slack") return `/api/hooks/providers/${catalogEntry.provider}`;
  return offersSlackEventsUrl(catalogEntry) ? "/api/v1/integrations/notifications/slack" : null;
}
