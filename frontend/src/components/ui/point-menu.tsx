// SPDX-License-Identifier: Apache-2.0

/**
 * PointMenu - a design-system `Menu` opened at a point instead of under a
 * button: the right click on a canvas node.
 *
 * The library's menu positions itself against its trigger, so the trigger is
 * a zero-size span placed at the point inside the caller's positioned
 * container; the menu is portalled and collision-aware like every other menu,
 * and it brings what a hand-rolled menu has to rebuild: `role="menu"`, focus
 * on the first item, Arrow/Home/End over the enabled items, Escape closing,
 * outside pointerdown dismissing. The rows are `OverflowMenuItems`,
 * so the same actions offered from a list row's kebab and from a canvas use
 * one item shape. It serves the fleet graph's node menu and the flow
 * builder's canvas, node and edge menu; there is no second menu
 * component.
 */
import { Menu, MenuContent, MenuTrigger } from "@filigran/design-system";

import { OverflowMenuItems, type OverflowMenuItem } from "./overflow-menu";

export interface PointMenuProps {
  /** Where to open, in the coordinate space of the nearest positioned ancestor; null closes it. */
  at: { x: number; y: number } | null;
  items: readonly OverflowMenuItem[];
  /** Accessible name of the menu. */
  label: string;
  onClose: () => void;
  /** Optional heading above the rows (e.g. "Handover type" over a set of options), drawn by `MenuHeader`;
   *  the `checked` rows right under it are grouped and named by it. */
  header?: string;
}

export function PointMenu({ at, items, label, onClose, header }: PointMenuProps) {
  if (!at) return null;
  return (
    <Menu open onOpenChange={(open: boolean) => !open && onClose()}>
      <MenuTrigger asChild>
        <span
          aria-hidden
          tabIndex={-1}
          data-testid="point-menu-anchor"
          className="pointer-events-none absolute size-0"
          style={{ left: at.x, top: at.y }}
        />
      </MenuTrigger>
      {/* The library names a menu after its trigger (`aria-labelledby`); this
          trigger is an empty, hidden anchor, so the menu takes its name from
          `label` instead. */}
      <MenuContent align="start" side="bottom" aria-label={label} aria-labelledby={undefined} className="min-w-[220px]">
        <OverflowMenuItems items={items} header={header} />
      </MenuContent>
    </Menu>
  );
}
