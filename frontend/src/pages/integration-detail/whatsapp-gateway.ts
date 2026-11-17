// SPDX-License-Identifier: Apache-2.0

import type { Integration } from "@/types";

/** The `backend` extra field of a `whatsapp` integration paired through a linked-device gateway. */
const WHATSAPP_GATEWAY_BACKEND = "linked_device_gateway";

/** Whether the integration is a `whatsapp` one on the linked-device gateway backend. */
export function isWhatsAppGateway(integration: Integration): boolean {
  return integration.provider === "whatsapp" && integration.extra_config?.backend === WHATSAPP_GATEWAY_BACKEND;
}
