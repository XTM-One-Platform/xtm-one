// SPDX-License-Identifier: Apache-2.0

/**
 * The inside of a `NavbarItem asChild` row.
 *
 * Under `asChild` the library renders the row's box but none of its content:
 * `Slot` cannot inject markup around the slotted router link or dialog
 * trigger, so the child brings its own icon and label. These are `NavbarItem`'s
 * own classes, which is what keeps a slotted row on the same icon axis and in
 * the same tones as the rows the library draws itself (a group heading, the
 * collapse toggle): the icon rests at the secondary tone and brightens with
 * the row, and while the rail is collapsed the label stays in the DOM as the
 * row's accessible name. OpenAEV carries the same body (`NavbarRowContent`).
 */
import type { ReactNode } from "react";
import { useNavbarCollapsed } from "@filigran/design-system";
import { cn } from "@/lib/utils";

export function NavRowBody({
  icon,
  children,
  badge,
  trailing,
}: {
  /** A 16px glyph. */
  icon: ReactNode;
  /** The label: the visible text while expanded, the accessible name always. */
  children: ReactNode;
  /** Pinned to the icon's corner (a status dot, the rail's lock). */
  badge?: ReactNode;
  /** After the label, while expanded only. */
  trailing?: ReactNode;
}) {
  const collapsed = useNavbarCollapsed();
  return (
    <>
      <span
        aria-hidden
        className={cn(
          // The row clips every direct child (its label-overflow rule), which
          // would cut a corner badge off; the icon holds no text to truncate.
          "relative inline-flex shrink-0 overflow-visible!",
          collapsed
            // `mr-0.5` balances the row's reserved left border, as the
            // library's own collapsed icons do.
            ? "mr-0.5 text-default-primary"
            : "text-default-secondary group-hover:text-default-primary group-focus-visible:text-default-primary group-[[aria-current=page]]:text-default-primary",
        )}
      >
        {icon}
        {badge}
      </span>
      <span className={cn("flex-1 truncate text-left", collapsed && "sr-only")}>{children}</span>
      {!collapsed && trailing}
    </>
  );
}
