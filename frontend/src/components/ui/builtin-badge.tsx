// SPDX-License-Identifier: Apache-2.0

/**
 * Built-in badge for platform-seeded entities.
 *
 * A built-in is full-synced from its seed on every boot, so it is read-only for
 * everyone — admins included — and an edit would be silently reverted by the
 * next deploy. The badge says so up front; the entity's "Duplicate" action is
 * the way to get an editable copy.
 *
 * Usage:
 *   <BuiltinBadge />           → default size for page headers
 *   <BuiltinBadge size="sm" /> → small icon for card headers; both use the caption type token
 */

import { Package } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { useFormatter } from "@/i18n";

interface BuiltinBadgeProps {
  size?: "sm" | "default";
}

export function BuiltinBadge({ size = "default" }: BuiltinBadgeProps) {
  const { t_i18n } = useFormatter();
  const iconClass = size === "sm" ? "h-2.5 w-2.5" : "h-3 w-3";
  return (
    <Badge variant="cyan" className="content-compact gap-1">
      <Package className={iconClass} /> {t_i18n("Built-in")}
    </Badge>
  );
}
