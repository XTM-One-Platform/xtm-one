// SPDX-License-Identifier: Apache-2.0

/**
 * Star toggle for favouriting an entity.
 *
 * A single component so the card grid and the list view of an index page share
 * one control: same icon, same hit area, same tooltip and same `aria-pressed`
 * semantics. Styling follows the skill "always load" pin — cyan when on, barely
 * there when off — so a filled accent icon keeps meaning "this is switched on"
 * everywhere in the app.
 *
 * The caller owns the state; this only renders it and reports clicks (the click
 * is swallowed so it never triggers a surrounding row/card link).
 */
import { Star } from "lucide-react";
import { cn } from "@/lib/utils";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";

export function FavoriteStar({
  active,
  onToggle,
  className,
}: {
  active: boolean;
  onToggle: () => void;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const label = active ? t_i18n("Remove from favorites") : t_i18n("Add to favorites");

  return (
    <Tip content={active ? t_i18n("Favorite: pinned to the top of the list") : t_i18n("Add to favorites")}>
      <button
        type="button"
        aria-label={label}
        aria-pressed={active}
        className={cn(
          "p-1 rounded-sm transition-colors shrink-0",
          active ? "text-accent" : "text-default-disabled hover:text-default-secondary",
          className,
        )}
        onClick={(e) => {
          // Cards and table rows are links: without this the toggle would also
          // navigate to the detail page.
          e.preventDefault();
          e.stopPropagation();
          onToggle();
        }}
      >
        <Star className={cn("h-3.5 w-3.5", active && "fill-current")} />
      </button>
    </Tip>
  );
}
