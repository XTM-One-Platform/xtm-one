// SPDX-License-Identifier: Apache-2.0

import type { Integration, ProviderCatalogItem } from "@/types";
import { usesAdminOAuthApp } from "./admin-oauth-app";
import { isWhatsAppGateway } from "./whatsapp-gateway";

/**
 * Whether at least one event of the provider can arrive by webhook (mechanism
 * webhook or hybrid). A provider whose events are all polled (NetSuite,
 * OpenCRQ) never calls the integration webhook URL, so no screen shows it.
 */
export function hasWebhookDeliveredEvents(catalogEntry: ProviderCatalogItem | undefined): boolean {
  return !!catalogEntry?.available_events.some((e) => e.mechanism !== "polling");
}

/** Which blocks the Webhooks card of the Configuration tab renders. */
interface WebhooksCardBlocks {
  /** Microsoft 365: events are polled, no webhook to configure. */
  microsoft: boolean;
  /** Slack: polled messages, plus the Events URL when the app offers it. */
  slack: boolean;
  /** Webhooks provisioned by the administrator's OAuth app. */
  adminEvents: boolean;
  /** The integration webhook URL, for events that can arrive by webhook. */
  webhookUrl: boolean;
  /** Notion's own setup for its managers: the URL, the verification token and the next step. */
  notionSetup: boolean;
  /** The provider callback URL of a developer app with no events. */
  callback: boolean;
  /** The GitHub repository webhook manager. */
  webhookManager: boolean;
}

/**
 * The blocks the Webhooks card renders for an integration. The card renders
 * nothing when none is shown, and the Configuration tab then gives the Event
 * Triggers card the full row instead of half of it with an empty column.
 */
export function webhooksCardBlocks(
  integration: Integration,
  catalogEntry: ProviderCatalogItem | undefined,
  canManage: boolean | undefined,
): WebhooksCardBlocks {
  const provider = integration.provider;
  // A WhatsApp integration on the linked-device gateway receives on its own gateway URL,
  // shown by the WhatsApp pairing card; the catalog path is the Meta backend's.
  const hasWebhookPath = !!catalogEntry?.webhook_path && !isWhatsAppGateway(integration);
  const hasEvents = !!catalogEntry && catalogEntry.available_events.length > 0;
  // The integration webhook URL only matters for events that can arrive by
  // webhook: the Event Triggers card already says polling for the others.
  const webhookEvents = hasWebhookDeliveredEvents(catalogEntry);
  const isAdminProvisioned = usesAdminOAuthApp(integration, catalogEntry);
  const isMicrosoft = provider === "microsoft";
  const isSlack = provider === "slack";
  // Slack message events reach new_slack_message assignments only through the
  // notifications endpoint, whichever Slack app sends them: the integration and
  // provider hooks key them as a plain message. So the Slack block never depends
  // on which OAuth app the integration uses.
  const hasOwnEventsBlock = isMicrosoft || isSlack;
  // A Notion manager gets Notion's own setup block (URL, verification token,
  // next step) in place of the generic URL block, whose content-type line does
  // not apply to Notion; anyone else keeps the generic URL block.
  const notionSetup = provider === "notion" && webhookEvents && !!canManage;
  return {
    microsoft: isMicrosoft,
    slack: isSlack,
    adminEvents: hasEvents && isAdminProvisioned && !hasOwnEventsBlock && !notionSetup,
    webhookUrl: webhookEvents && !isAdminProvisioned && !hasOwnEventsBlock && !notionSetup,
    notionSetup,
    callback: hasWebhookPath && !hasEvents && !isMicrosoft,
    webhookManager: !!catalogEntry?.supports_webhook_management && integration.status === "connected" && !!canManage,
  };
}

/** Whether any of the blocks is shown, i.e. the Webhooks card renders at all. */
export function showsAnyWebhooksBlock(blocks: WebhooksCardBlocks): boolean {
  return Object.values(blocks).some(Boolean);
}

/** Whether the Webhooks card renders anything at all for the integration. */
export function webhooksCardShowsBlock(
  integration: Integration,
  catalogEntry: ProviderCatalogItem | undefined,
  canManage: boolean | undefined,
): boolean {
  return showsAnyWebhooksBlock(webhooksCardBlocks(integration, catalogEntry, canManage));
}
