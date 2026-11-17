// SPDX-License-Identifier: Apache-2.0

/** Shared channel icon — resolves a channel routing type to its brand logo or Lucide icon. */

import { MessageSquare } from "lucide-react";
import { cn } from "@/lib/utils";
import { CHANNEL_ICONS, CHANNEL_ROUTING_ICON_KEY } from "@/lib/constants";

export function ChannelIcon({ channel, className }: { channel: string; className?: string }) {
  const iconKey = CHANNEL_ROUTING_ICON_KEY[channel] ?? channel;
  const config = iconKey ? CHANNEL_ICONS[iconKey] : null;

  // Prefer brand SVG logo when available
  if (config?.logo) {
    return <img src={config.logo} alt={channel} className={cn("h-3.5 w-3.5 object-contain", className)} />;
  }

  const Icon = config?.icon ?? MessageSquare;
  return <Icon className={cn("h-3.5 w-3.5", config?.fg, className)} />;
}
