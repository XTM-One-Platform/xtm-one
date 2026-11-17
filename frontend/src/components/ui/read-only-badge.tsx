// SPDX-License-Identifier: Apache-2.0

/**
 * Read-only badge for an entity the viewer cannot edit: company-managed (for
 * non-admins), built-in, or reached through a read-only share - or for a
 * setting the platform fixes (`tooltip` says why).
 *
 * Usage:
 *   <ReadOnlyBadge />           → default size (10px) for page headers
 *   <ReadOnlyBadge size="sm" /> → small size (9px) for card headers
 */

import { Lock } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";

interface ReadOnlyBadgeProps {
  size?: "sm" | "default";
  /** Why it is read-only, shown on hover (already translated). */
  tooltip?: string;
}

export function ReadOnlyBadge({ size = "default", tooltip }: ReadOnlyBadgeProps) {
  const { t_i18n } = useFormatter();
  const textClass = size === "sm" ? "text-[9px]" : "content-caption";
  return (
    <Tip content={tooltip}>
      <Badge variant="outline" className={`${textClass} gap-1`}>
        <Lock className="h-2.5 w-2.5" /> {t_i18n("Read-only")}
      </Badge>
    </Tip>
  );
}
