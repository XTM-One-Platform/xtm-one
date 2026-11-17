// SPDX-License-Identifier: Apache-2.0

import { Users } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { IconMark } from "@/components/ui/icon-mark";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";

/**
 * Shared badge — displayed on entities shared with the current user via group
 * sharing (not owned by the user, not company-managed).
 *
 * @param size "sm" for list-card badges (smaller icon), "default" for detail-page
 *   badges; both read at content-compact (12 px, design standards gate 7).
 * @param label Custom label text. Defaults to "Shared".
 * @param ownerName Name of the user who owns/shared the entity.
 */
export function SharedBadge({
  size = "sm",
  label = "Shared",
  ownerName,
  full = false,
}: {
  size?: "sm" | "default";
  label?: string;
  ownerName?: string | null;
  /** Show the whole owner name, wrapping instead of truncating: where whose
   *  it is decides whether to trust it (the investigation source picker). */
  full?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const text = ownerName ? t_i18n("Shared by {name}", { name: ownerName }) : t_i18n(label);
  const badge = (
    <Badge className={`content-compact ${full ? "max-w-full" : "max-w-[120px]"} border-filigran-ia-primary/30 bg-filigran-ia-primary/10 text-filigran-ia-primary light:border-filigran-ia-primary/40 light:bg-filigran-ia-primary/10 light:text-filigran-ia-primary`}>
      <Users className={`${size === "sm" ? "h-2.5 w-2.5" : "h-3 w-3"} shrink-0 mr-0.5`} />
      <span className={full ? "break-words" : "truncate"}>{text}</span>
    </Badge>
  );

  return (
    <Tip content={ownerName ? t_i18n("Owned and shared by {name}", { name: ownerName }) : text}>
      {badge}
    </Tip>
  );
}

/**
 * Icon-only variant — the counterpart to `CompanyManagedIcon` for the tight
 * icon rows on entity cards, where the "Shared by …" text badge squeezes the
 * content beside it. The tooltip carries the wording, including the owner;
 * the shared `IconMark` trigger makes it a tab stop with a focus ring.
 */
export function SharedIcon({ ownerName }: { ownerName?: string | null }) {
  const { t_i18n } = useFormatter();
  const text = ownerName
    ? t_i18n("Owned and shared by {name}", { name: ownerName })
    : t_i18n("Shared");
  return (
    <IconMark label={text}>
      <Users className="h-3.5 w-3.5 shrink-0 text-filigran-ia-primary light:text-filigran-ia-primary" aria-hidden="true" />
    </IconMark>
  );
}
