// SPDX-License-Identifier: Apache-2.0

/**
 * VisibilityFilter: the one ownership quick filter of every list page
 * (dev:visibility-filters-consistency, `lib/visibility.ts`).
 *
 * Four segments, always in this order and wording: Mine | Company | Shared |
 * All. Exactly one is selected (a radio group: arrow keys move and select,
 * Home / End jump to the ends, one tab stop). A page passes `hidden` for a
 * segment its objects cannot hold, never another set, and `counts` when it
 * knows how many items each segment holds.
 *
 * VisibilityEmptyHint is the line a page shows when Mine is empty while other
 * segments are not: what is elsewhere, and a link to All. Otherwise it renders
 * its children, the page's own "nothing matches" line.
 */

import { Building2, Layers, User, Users } from "lucide-react";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { VISIBILITY_SEGMENTS, type Visibility } from "@/lib/visibility";

interface VisibilityFilterProps {
  value: Visibility;
  onChange: (value: Visibility) => void;
  /** Segments this page cannot hold (e.g. `["company"]`); the others keep their order. */
  hidden?: readonly Visibility[];
  /** How many items each segment holds, shown after its label when known (`null`: the read failed, no number). */
  counts?: Partial<Record<Visibility, number>> | null;
  className?: string;
  /** A compact toolbar: each segment shows its icon and count, the label kept as its accessible name and tooltip. */
  iconOnly?: boolean;
  /** `menu`: one row that never wraps, the segments that do not fit in its "More tabs" menu (a filter row that must stay one line); default `wrap`. */
  overflow?: "wrap" | "menu";
  /** The list is on its first read: each count is a placeholder, never "0" (UX r3 B5 on #11349). */
  pending?: boolean;
  /** A segment's own tooltip (already translated), in place of its generic hint: e.g. Company's built-ins hidden (#11831). */
  tips?: Partial<Record<Visibility, React.ReactNode>>;
}

/** The label of each segment, read by `SegmentedFilter` through `t_i18n`. @i18n-keys */
const SEGMENT_LABEL = {
  mine: "Mine",
  company: "Company",
  shared: "Shared",
  all: "All",
} as const satisfies Record<Visibility, string>;

/** What each segment holds, the segment's tooltip. @i18n-keys */
const SEGMENT_HINT = {
  mine: "Items you own",
  company: "Items published to the whole company",
  shared: "Items shared with you directly or through a group",
  all: "Everything you can access",
} as const satisfies Record<Visibility, string>;

const SEGMENT_ICON: Record<Visibility, React.ElementType> = {
  mine: User,
  company: Building2,
  shared: Users,
  all: Layers,
};

/** The four segments, drawn by the shared `SegmentedFilter` as a radio group. */
export function VisibilityFilter({ value, onChange, hidden, counts, className, iconOnly, overflow, pending = false, tips }: VisibilityFilterProps) {
  const { t_i18n } = useFormatter();
  const options = VISIBILITY_SEGMENTS.filter((segment) => !hidden?.includes(segment)).map((segment) => ({
    key: segment,
    label: SEGMENT_LABEL[segment],
    hint: SEGMENT_HINT[segment],
    tip: tips?.[segment],
    icon: SEGMENT_ICON[segment],
    count: pending ? undefined : counts?.[segment],
    countPending: pending && counts?.[segment] !== undefined,
  }));
  return (
    <SegmentedFilter
      options={options}
      value={value}
      onChange={onChange}
      selection="radio"
      ariaLabel={t_i18n("Visibility")}
      testId="visibility-filter"
      className={className}
      iconOnly={iconOnly}
      overflow={overflow}
    />
  );
}

interface VisibilityEmptyHintProps {
  /** The segment shown. The hint only renders on Mine. */
  value: Visibility;
  /**
   * How many items Mine and All hold UNDER THE PAGE'S OTHER FILTERS (search,
   * tags, properties, scopes); the hint renders when Mine holds none and All
   * some, so a search that matches nothing keeps the page's own line.
   */
  counts: Partial<Record<Visibility, number>> | undefined;
  onShowAll: () => void;
  /** What the page shows otherwise (its "nothing matches the filters" line). */
  children?: React.ReactNode;
  className?: string;
}

/** On an empty Mine: one line saying the other segments hold items, and a link to All. */
export function VisibilityEmptyHint({ value, counts, onShowAll, children, className }: VisibilityEmptyHintProps) {
  const { t_i18n } = useFormatter();
  if (value !== "mine" || counts?.mine !== 0 || !counts.all) return <>{children}</>;
  return (
    <p className={cn("py-6 text-center content-base text-default-secondary", className)} data-testid="visibility-empty-hint">
      {t_i18n("Nothing of yours here yet. Other people's items you can access are under Company, Shared and All.")}{" "}
      <button
        type="button"
        onClick={onShowAll}
        className="font-medium text-accent underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        {t_i18n("Show all")}
      </button>
    </p>
  );
}
