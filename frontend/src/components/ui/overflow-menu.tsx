// SPDX-License-Identifier: Apache-2.0

/**
 * OverflowMenu — the three-dot ("kebab") menu, on the design system's `Menu`.
 *
 * This used to be hand-rolled, for a reason the file stated plainly:
 * `@radix-ui/react-dropdown-menu` was not a dependency, and pulling one in for a
 * single short menu was a heavier change than the menu itself. The design system
 * depends on it and exposes `Menu`, so that premise is gone — and with it about
 * 200 lines that had to implement, by hand, everything a dropdown owes its user:
 * `aria-haspopup`/`aria-expanded`, `role="menu"`/`menuitem`, Arrow/Home/End
 * cycling over the enabled items only, Escape closing and restoring focus to the
 * trigger (captured, so an enclosing dialog does not swallow it first),
 * outside `pointerdown` rather than `click` so a drag starting
 * elsewhere dismisses it, and focusing the first item on keyboard-open but not
 * on mouse-open. All of that is the library's now.
 *
 * The item shape (`OverflowMenuItem`) is kept so call sites are untouched: they
 * describe a menu as data, which is a nicer thing to write than a tree of
 * compound components, and it keeps the `destructive` flag meaningful.
 */

import { Fragment, useId, type ReactNode } from "react";
import { Button, Icon, IconButton, Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger, type IconName } from "@filigran/design-system";
import { cn } from "@/lib/utils";

export interface OverflowMenuItem {
  /** Stable key + test handle. */
  id: string;
  label: string;
  icon?: React.ElementType;
  /** A design system icon name, drawn by its `Icon` at 16 px; wins over `icon`. */
  glyph?: IconName;
  onSelect: () => void;
  disabled?: boolean;
  /** Why the item is disabled, shown under it while it is: an action refused
   *  for a reason says the reason where the person looks, never only after a
   *  click. Rendered outside the dimmed disabled item so it keeps its full
   *  contrast, and tied to the item with `aria-describedby`. Ignored while
   *  the item is enabled. */
  disabledReason?: string;
  /** Renders the item in the destructive colour (delete-style actions). */
  destructive?: boolean;
  /** Keyboard shortcut shown right-aligned as a `<kbd>` hint (display only —
   *  the binding itself belongs to whoever owns the surface). */
  shortcut?: string;
  /** Draws a divider above the item: the start of a new group. */
  separatorBefore?: boolean;
  /** One option of a set where one is current (the flow builder's handover
   *  type): the row is a `menuitemradio`, `aria-checked` says which, and the
   *  current one shows a dot in the icon slot. Undefined: a plain action. */
  checked?: boolean;
}

interface OverflowMenuProps {
  items: OverflowMenuItem[];
  /** Accessible name for the trigger button. */
  label: string;
  /** Extra classes for the trigger button. */
  className?: string;
  /** Optional heading above the rows (e.g. "Insert after"), drawn by `MenuHeader`. */
  header?: string;
  /**
   * Custom trigger content (icon and/or text). Defaults to the three-dot icon;
   * pass e.g. a Download icon + "Export" label to reuse the menu behaviour for
   * a labelled action menu. When set, the trigger is sized by its content
   * instead of the fixed 8x8 square.
   */
  trigger?: ReactNode;
  /** Show a busy state on the trigger (disables it). */
  busy?: boolean;
  /**
   * Size of a labelled trigger, the design system's button size: set it when
   * the menu sits next to other buttons of a header so the row keeps one
   * height ("sm" beside a `size="sm"` Edit). Unset: the 32px toolbar trigger.
   */
  size?: "sm" | "md";
  /**
   * Weight of a labelled trigger, the design system's button priority:
   * "secondary" draws the outline of the `variant="outline"` buttons, so a menu
   * standing for a create action looks like the plain Create buttons beside it
   * (the hub cards). Unset: the borderless tertiary trigger.
   */
  priority?: "secondary" | "tertiary";
  /**
   * Open the menu above the trigger (a trigger low on its surface) instead of
   * below it. `auto` lets the menu choose: it opens below the trigger and
   * flips above when the viewport has no room there. The library's menu is
   * portalled and collision-aware on every side, so no scrolling list or
   * clipping panel around the trigger can cut it whichever value is passed.
   */
  side?: "bottom" | "top" | "auto";
  /** Bound the menu's height and scroll its items (a long list, such as a block palette). */
  scroll?: boolean;
  /**
   * Render the menu where the trigger is instead of portalling it to
   * `document.body`. For a trigger inside an element that may go fullscreen
   * (the deck's bar): the browser paints only the fullscreen element's own
   * subtree, so a body-portalled menu opens invisibly - and a bar that hides
   * when idle keeps itself up only for a menu it contains. The library's menu
   * still positions itself with fixed coordinates, so an overflowing parent
   * does not clip it.
   */
  inline?: boolean;
}

export function OverflowMenu({ items, label, className, header, trigger, busy = false, size, priority = "tertiary", side = "bottom", scroll = false, inline = false }: OverflowMenuProps) {
  return (
    <Menu>
      {/* The trigger is a library button the menu composes onto (`asChild`):
          without it Radix renders a bare native <button> that no design-system
          style or focus policy reaches, and the library says so in a dev
          warning. A labelled trigger is a tertiary `Button` sized by its
          content; the icon-only default an `IconButton` held to the 32px square
          the entity cards line up to. */}
      <MenuTrigger asChild>
        {trigger ? (
          <Button
            variant="default"
            priority={priority}
            aria-label={label}
            aria-busy={busy || undefined}
            disabled={busy}
            size={size}
            className={cn(size ? "gap-1.5" : "h-8 gap-1.5 px-2 content-compact", className)}
          >
            {trigger}
          </Button>
        ) : (
          <IconButton
            variant="default"
            priority="tertiary"
            aria-label={label}
            aria-busy={busy || undefined}
            disabled={busy}
            icon={<Icon name="ellipsis-vertical" size={16} />}
            className={cn("size-8", className)}
          />
        )}
      </MenuTrigger>
      <MenuContent portalled={!inline} align="end" side={side === "auto" ? "bottom" : side} className={cn("min-w-[220px]", scroll && "max-h-80 overflow-y-auto")}>
        <OverflowMenuItems items={items} header={header} />
      </MenuContent>
    </Menu>
  );
}

/**
 * The heading of a design-system menu, above its rows: one style for every
 * menu, set on the row inset (`px-4`) so its first letter lines up with
 * the row icons instead of starting half an inset to their left. Callers pass a
 * string (`header` on `OverflowMenu` and `PointMenu`), never their own markup.
 * `id` lets the group of options under it take it as its name.
 */
function MenuHeader({ children, id }: { children: string; id?: string }) {
  return (
    <p id={id} data-testid="menu-header" className="px-4 pb-1 pt-1.5 content-caption uppercase tracking-wide text-default-secondary">
      {children}
    </p>
  );
}

/**
 * The rows of a design-system menu drawn from `OverflowMenuItem` data. Shared by
 * the kebab menu above and the menu opened at a point (`PointMenu`), so a
 * surface that offers the same actions from a row and from a canvas describes
 * them once.
 *
 * With a `header`, the menu's heading is drawn above the rows by `MenuHeader`;
 * when the rows under it start with a set of options (`checked` rows, the
 * flow builder's handover type), those options sit in a `role="group"` named
 * by the header (`aria-labelledby`), so a screen reader announces the set's
 * name with its options. The group is a plain block: no visual change.
 */
export function OverflowMenuItems({ items, header }: { items: readonly OverflowMenuItem[]; header?: string }) {
  const headerId = useId();
  if (!header) return <>{items.map((item, index) => renderRow(item, index))}</>;
  let options = 0;
  while (options < items.length && items[options].checked !== undefined) options += 1;
  return (
    <>
      <MenuHeader id={headerId}>{header}</MenuHeader>
      {options > 0 ? (
        <div role="group" aria-labelledby={headerId} data-testid="menu-header-group">
          {items.slice(0, options).map((item, index) => renderRow(item, index))}
        </div>
      ) : null}
      {items.slice(options).map((item, index) => renderRow(item, options + index))}
    </>
  );
}

/** One row of `OverflowMenuItems`; `index` is its place in the whole list. */
function renderRow(item: OverflowMenuItem, index: number) {
  const RowIcon = item.icon;
  const radio = item.checked !== undefined;
  const reasonShown = !!item.disabled && !!item.disabledReason;
  const reasonId = `overflow-menu-item-${item.id}-reason`;
  return (
    <Fragment key={item.id}>
      {item.separatorBefore && index > 0 && <MenuSeparator />}
      <MenuItem
        disabled={item.disabled}
        aria-describedby={reasonShown ? reasonId : undefined}
        data-testid={`overflow-menu-item-${item.id}`}
        // Spread only for an option: a `role={undefined}` prop would
        // override the library's own `menuitem` role.
        {...(radio ? { role: "menuitemradio", "aria-checked": item.checked } : {})}
        startIcon={
          item.checked ? (
            <span aria-hidden className="flex size-4 shrink-0 items-center justify-center">
              <span className="size-1.5 rounded-full bg-accent" />
            </span>
          ) : item.glyph ? (
            <Icon name={item.glyph} size={16} className={cn(item.destructive && "text-feedback-error-tertiary")} />
          ) : RowIcon ? (
            // A destructive row's icon takes the label's token, so icon and
            // label read as one destructive row.
            <RowIcon className={cn("size-4 shrink-0", item.destructive && "text-feedback-error-tertiary")} />
          ) : undefined
        }
        // Display only, so it goes in the trailing slot rather than
        // becoming a second control: the binding belongs to whoever
        // owns the surface, not to the menu row.
        endIcon={
          item.shortcut ? (
            <kbd className="ml-3 shrink-0 rounded-sm border border-elevation-subtle-soft bg-elevation-default px-1.5 py-0.5 font-sans-plex content-caption leading-none text-default-secondary">
              {item.shortcut}
            </kbd>
          ) : undefined
        }
        // Radix closes the menu itself before running this, so a handler
        // that opens a dialog no longer leaves the menu painted under it —
        // the reason the hand-rolled version closed explicitly first.
        onSelect={item.onSelect}
        // The tertiary error token, not the primary: on the menu panel and its
        // hover step the primary reads 4.26:1 in dark (red-400 on grayblue-700)
        // and 4.08:1 on a hovered light row (red-600 on gray-200), under AA for
        // 14 px text. The tertiary is the same family a step away from the
        // surface in each theme (red-300 dark, red-700 light): 5.05:1 or more on
        // every panel and hover layer (held by
        // tests_unit/destructive-menu-row-contrast.test.ts).
        className={cn(item.destructive && "text-feedback-error-tertiary")}
      >
        <span className="min-w-0 flex-1 truncate">{item.label}</span>
      </MenuItem>
      {reasonShown && (
        <p
          id={reasonId}
          data-testid={`overflow-menu-item-${item.id}-reason`}
          className="max-w-xs px-3 pb-2 whitespace-normal content-caption text-default-secondary"
        >
          {item.disabledReason}
        </p>
      )}
    </Fragment>
  );
}
