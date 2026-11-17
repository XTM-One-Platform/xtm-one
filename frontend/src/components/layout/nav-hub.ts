// SPDX-License-Identifier: Apache-2.0

/**
 * The pure half of the hubs' single source (#9301 N2c, UX r1 B3): find a
 * group of the resolved menu, and turn its visible children into hub cards.
 * Kept free of React and of the `@/` alias so the node tests run it for
 * every role x mode x flag set x AI state (`use-nav-group.test.ts`); the hook
 * that resolves the menu for the current reader is `use-nav-group.ts`.
 */
import type { HubResource } from "../ui/resource-hub";
import type { NavGroup, NavLeaf, NavSection } from "./nav-model.ts";

/** One group of a resolved nav, or `null` when the reader sees none of it. */
export function findNavGroup(sections: NavSection[], id: string): NavGroup | null {
  for (const section of sections) {
    for (const entry of section.entries) {
      if (entry.kind === "group" && entry.id === id) return entry;
    }
  }
  return null;
}

/**
 * The family row whose members include `to`, as this reader resolves it, or
 * `null` when the reader sees that page alone (a group manager's Model usage)
 * or it belongs to no family: the page then keeps its own title.
 */
export function findNavFamily(sections: NavSection[], to: string): NavLeaf | null {
  for (const section of sections) {
    for (const entry of section.entries) {
      /* A family is a section's row (Observability) or a group's (Connections). */
      const rows = entry.kind === "leaf" ? [entry] : entry.children;
      const row = rows.find((leaf) => leaf.family?.some((member) => member.to === to));
      if (row) return row;
    }
  }
  return null;
}

/**
 * The pages of a group in its order, a family row flattened into its members
 * (UX r1 B5 of #9301: Connections gives three cards, one per page), each
 * member carrying its row's lock.
 */
export function hubPages(group: NavGroup): NavLeaf[] {
  return group.children.flatMap((child) => (
    child.family
      ? child.family.map((member): NavLeaf => ({
        kind: "leaf",
        to: member.to,
        label: member.label,
        icon: member.icon,
        ...(child.locked ? { locked: true, lockReason: child.lockReason } : {}),
      }))
      : [child]
  ));
}

/** What a hub knows of one of its pages, keyed by the page's route. */
type HubCardData = Omit<HubResource, "to" | "icon" | "label" | "locked" | "lockReason">;

/**
 * One card per visible page of `group` (`hubPages`), in its order, with its
 * icon, label and lock; the data comes from `data[child.to]`. `null` declares a child
 * that is not a card of this hub (Value in Work: a report, not a collection).
 * A visible child with no entry at all is a hub that drifted from the menu:
 * it throws, so the hub test fails instead of a card going missing.
 */
export function hubCards(group: NavGroup | null, data: Record<string, HubCardData | null>): HubResource[] {
  if (!group) return [];
  return hubPages(group).flatMap((child: NavLeaf) => {
    const entry = data[child.to];
    if (entry === null) return [];
    if (!entry) throw new Error(`hub "${group.id}" has no card data for its child ${child.to}`);
    return [{
      ...entry,
      to: child.to,
      icon: child.icon,
      label: child.label,
      ...(child.locked ? { locked: true, lockReason: child.lockReason } : {}),
    }];
  });
}
