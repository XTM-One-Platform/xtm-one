// SPDX-License-Identifier: Apache-2.0

/**
 * The header of an entity page: back link, icon tile, title with its
 * "i" hint, description, badges, a links row, and the actions on the right.
 *
 * The workspace page and the fleet page drew two different title blocks (a
 * 24 px and a 30 px h1, different spacing) for two halves of one model. Both
 * render this one, and so do the agent, group and knowledge base pages
 * and every other entity detail page;
 * `entity-page-header.test.tsx` keeps a page from drawing its own h1 next to
 * a BackButton again, and every back label in the "Back to <Parent>" form.
 *
 * Narrow viewports: a page with more than three header actions keeps
 * its primary action and its toggle in the row and folds the secondary ones
 * into a "More actions" menu below the `xl` breakpoint (1280 px). The page
 * wraps those buttons in `EntityHeaderWideOnly` and lists the same actions as
 * menu rows in `EntityHeaderMoreActions`; at 1280 px and above the row is
 * unchanged. Both are CSS-switched (no resize listener), so the menu and the
 * buttons never show together.
 *
 * Density (issue #10215, header and hero density sweep): the description is
 * clamped to two lines with a "Read more" / "Show less" toggle that appears
 * only when the text is cut, so a long (often AI-written) description never
 * pushes the tabs down and is never lost; badges, meta facts and links share
 * ONE meta row under the title instead of a row each.
 *
 * Height budget (#10215, UX round 1 B1): at most 140 px on an entity page
 * at 1440 px, from the back link to the first block under the header. The
 * toggle sits at the end of the second line (never a row of its own), the
 * back link is 4 px above the title row, title, description and meta row
 * are 2 px apart, and the header ends 16 px above
 * the next block (`mb-4`, which wins over the 24 px of the page's
 * `space-y-6` stack: Tailwind 4 writes that margin under `:where`).
 * `tests_e2e/entity-header-budget.spec.ts` measures it on a fleet and an
 * agent page.
 *
 * `stackActions`: the action group takes its own row under the text, which
 * then gets the full width (the description keeps its two-line clamp). A
 * page whose column lost its room to a docked panel below 1280 px asks for it
 * (the fleet page with Modify with AI open, UX round 2 B1 on #10181), so the
 * title never shrinks to a narrow column beside the buttons.
 *
 * `actionsBesideTitle`: the action group shares the title's line only; the
 * description and the meta row run under both at the full width of the
 * header. For a column a docked panel narrowed at 1280 px and above (the
 * fleet page with Modify with AI open at 1440, UX round 4 B1 on #10181): the
 * text keeps the width the buttons would take, so a long meta row or a
 * description never grows the header past its 140 px budget, and no row is
 * added. The title is one line there (`wrapTitle` is the page's to drop).
 */
import type { ElementType, ReactNode } from "react";
import { isValidElement, useId, useLayoutEffect, useRef, useState } from "react";

import { BackButton } from "@/components/ui/back-button";
import { InfoTip } from "@/components/ui/info-tip";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

/** Header actions shown at 1280 px and above only; below, the same actions
 *  are rows of the page's `EntityHeaderMoreActions` menu. */
export function EntityHeaderWideOnly({ children }: { children: ReactNode }) {
  return (
    <span className="hidden xl:contents" data-testid="entity-page-header-wide-only">
      {children}
    </span>
  );
}

/** The "More actions" menu holding the secondary header actions below
 *  1280 px; hidden at 1280 px and above, where they are buttons. Renders
 *  nothing without an item. `always`: shown at every width, for a page whose
 *  row lost room to a docked panel (the page then leaves the wide-only
 *  buttons out). `menuOnly`: rows that have no button in the wide row (a
 *  rare or destructive action): the menu then stays at 1280 px and above
 *  with those rows alone, so they are reachable at every width (#1508 UX
 *  round 1 B1); below 1280 px they follow `items` in the one menu. */
export function EntityHeaderMoreActions({
  items,
  always = false,
  menuOnly = [],
}: {
  items: OverflowMenuItem[];
  always?: boolean;
  menuOnly?: OverflowMenuItem[];
}) {
  const { t_i18n } = useFormatter();
  const all = [...items, ...menuOnly];
  if (all.length === 0) return null;
  if (!always && menuOnly.length > 0) {
    return (
      <>
        <span className="contents xl:hidden" data-testid="entity-page-header-more-actions" data-widths="below-xl">
          <OverflowMenu label={t_i18n("More actions")} items={all} />
        </span>
        <span className="hidden xl:contents" data-testid="entity-page-header-more-actions-wide" data-widths="xl">
          <OverflowMenu label={t_i18n("More actions")} items={menuOnly} />
        </span>
      </>
    );
  }
  const shown = always ? all : items;
  if (shown.length === 0) return null;
  return (
    <span
      className={always ? "contents" : "contents xl:hidden"}
      data-testid="entity-page-header-more-actions"
      data-widths={always ? "all" : "below-xl"}
    >
      <OverflowMenu label={t_i18n("More actions")} items={shown} />
    </span>
  );
}

export function EntityPageHeader({
  backTo,
  backLabel,
  icon: Icon,
  leading,
  title,
  hint,
  hintLabel,
  description,
  badges,
  meta,
  links,
  actions,
  muted,
  tileClassName,
  titleClassName,
  wrapTitle = false,
  stackActions = false,
  actionsBesideTitle = false,
  metaOneLine = false,
  className,
}: {
  backTo: string | number;
  /** An i18n key, translated by BackButton. */
  backLabel: string;
  /** A lucide icon component, or a node of the page's own (a provider logo,
   *  an avatar) drawn in the tile as it is. Omitted: no tile (a page whose
   *  title never had one, a trace or a run comparison). */
  icon?: ElementType | ReactNode;
  /** A picture of the page's own drawn where the tile goes, without the
   *  tile (the CTEM stage's posture gauge). It sits in the header row, so
   *  the back link stays first, at the content edge, as on every other
   *  page. Takes the place of `icon` when both are given. */
  leading?: ReactNode;
  title: string;
  /** The model sentence shown on the "i" beside the title. */
  hint?: ReactNode;
  /** Names the "i" for assistive technology. */
  hintLabel?: string;
  description?: string | null;
  badges?: ReactNode;
  /** A row of facts about the entity (owner, slug), under the badges. */
  meta?: ReactNode;
  /** The cross links under the title (e.g. `FleetWorkspaceChip`). */
  links?: ReactNode;
  actions?: ReactNode;
  /** Draws the icon tile in the neutral tone (a disabled entity). */
  muted?: boolean;
  /** The tile's tone when the page states one (a status colour); replaces
   *  the accent or muted background. */
  tileClassName?: string;
  /** Extra classes on the h1 (a page font such as `font-sans-geologica`). */
  titleClassName?: string;
  /** The title wraps to two lines instead of one truncated line (a long user-given name). */
  wrapTitle?: boolean;
  /** The actions on their own row under the text, the description clamped to two lines. */
  stackActions?: boolean;
  /** The actions on the title's line only, the description and meta row under both at full width. */
  actionsBesideTitle?: boolean;
  /** The meta row never wraps: its badges keep their width (`shrink-0`) and
   *  its named facts shrink and truncate with their full text on hover
   *  (`min-w-0 truncate` + `title`), so the row stays one line at 1024 px
   *  (UX round 2 B1 on #10287). */
  metaOneLine?: boolean;
  className?: string;
}) {
  const hasTile = (leading != null && leading !== false) || (Icon != null && Icon !== false);
  /* EntityPageHeaderSkeleton below draws this same box with its content greyed out: a class changed here changes there. */
  // One grid: tile | title | actions on the first line, description and meta under title and actions.
  const beside = actionsBesideTitle && !stackActions;
  return (
    <div className={cn(HEADER_CLASS, className)} data-testid="entity-page-header">
      <BackButton to={backTo} label={backLabel} />
      {/* One row that never wraps: the text column takes the width
          left and wraps its description, the actions stay top right on the
          title row whatever the description length or the viewport, and the
          icon tile stays beside the title instead of centring on the text. */}
      <div
        className={cn(
          beside
            ? cn("grid items-start gap-x-4 gap-y-0.5", hasTile ? "grid-cols-[auto_minmax(0,1fr)_auto]" : "grid-cols-[minmax(0,1fr)_auto]")
            : "flex",
          !beside && (stackActions ? "flex-col items-stretch gap-3" : "items-start justify-between gap-4"),
        )}
        data-testid="entity-page-header-row"
        data-actions-placement={stackActions ? "stacked" : beside ? "title" : "row"}
      >
        <div className={beside ? "contents" : "flex min-w-0 flex-1 items-start gap-4"} data-testid="entity-page-header-text">
          {leading != null && leading !== false ? (
            <div className={cn("shrink-0", beside && "col-start-1 row-span-3 row-start-1")} data-testid="entity-page-header-leading">
              {leading}
            </div>
          ) : Icon != null && Icon !== false && (
            <div
              data-testid="entity-page-header-tile"
              data-tone={tileClassName ? "page" : muted ? "muted" : "accent"}
              className={cn(
                TILE_CLASS,
                beside && "col-start-1 row-span-3 row-start-1",
                tileClassName ?? (muted ? "bg-elevation-hover" : "bg-accent/10"),
              )}
            >
              {isValidElement(Icon) ? Icon : <IconComponent icon={Icon as ElementType} muted={muted} />}
            </div>
          )}
          {/* 2 px between title, description and meta row: the fleet page's
              badge and chip row measured 142 px at 4 px (CI, #10217). */}
          <div className={beside ? "contents" : "min-w-0 space-y-0.5"}>
            <div
              className={cn(
                "flex min-w-0 items-center gap-2",
                // The title line is as tall as the 36 px actions beside it, less 2 px of the row gap.
                beside && (hasTile ? "col-start-2 row-start-1 min-h-[34px]" : "col-start-1 row-start-1 min-h-[34px]"),
              )}
            >
              {/* A long name wraps to two lines (fr at 1024) where the page asks for it;
                  past them, and on a one-line title, the full name is on hover. */}
              <h1
                className={cn(wrapTitle ? "line-clamp-2 break-words" : "truncate", TITLE_CLASS, titleClassName)}
                title={title}
                data-wrap={wrapTitle ? "true" : undefined}
              >
                {title}
              </h1>
              {hint && <InfoTip content={hint} label={hintLabel ?? title} />}
            </div>
            {description && (
              <EntityHeaderDescription
                text={description}
                className={beside ? (hasTile ? "col-start-2 col-end-4 row-start-2" : "col-start-1 col-end-3 row-start-2") : undefined}
              />
            )}
            {(badges || meta || links) && (
              /* One meta row: badges, facts and cross links side by side, wrapping
                 only when the width runs out (#10215). */
              <div
                className={cn(
                  "flex items-center gap-x-4 gap-y-1",
                  metaOneLine ? "min-w-0 flex-nowrap" : "flex-wrap",
                  beside && (hasTile ? "col-start-2 col-end-4 row-start-3" : "col-start-1 col-end-3 row-start-3"),
                )}
                data-testid="entity-page-header-meta"
                data-one-line={metaOneLine ? "true" : undefined}
              >
                {badges && (
                  <span className="contents" data-slot="badges">
                    {badges}
                  </span>
                )}
                {meta && (
                  <span className="contents content-compact text-default-secondary" data-slot="meta">
                    {meta}
                  </span>
                )}
                {links && (
                  <span className="contents" data-slot="links">
                    {links}
                  </span>
                )}
              </div>
            )}
          </div>
        </div>
        {actions && (
          <div
            className={cn(
              "flex items-center gap-2",
              stackActions ? "flex-wrap" : "shrink-0",
              // Its 36 px buttons overhang the 34 px title line by the 2 px row gap, never the description.
              beside && (hasTile ? "col-start-3 row-start-1 -mb-0.5" : "col-start-2 row-start-1 -mb-0.5"),
            )}
            data-testid="entity-page-header-actions"
          >
            {actions}
          </div>
        )}
      </div>
    </div>
  );
}

/** The description, two lines at most; the toggle shows only when the
 *  clamp cuts the text, and opens it in place (full text, nothing lost).
 *
 *  Clamped, the toggle sits on the second line only (one line tall, `1lh`),
 *  over its end on the page background, with a fade into it so the cut word
 *  dissolves instead of being shaved; it adds no row and never covers line
 *  one (UX round 2 B1). Expanded, the paragraph turns inline and the toggle
 *  follows its last word. Its hit area is 24 px tall either way, from a
 *  transparent `::before`, not from the visible box.
 *
 *  One button instance stays mounted in both states, at the same place in
 *  the tree, so activating it never drops the keyboard focus (UX round 2 B2). */
function EntityHeaderDescription({ text, className }: { text: string; className?: string }) {
  const { t_i18n } = useFormatter();
  const id = useId();
  const ref = useRef<HTMLParagraphElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || expanded) return;
    const measure = () => setOverflows(el.scrollHeight > el.clientHeight + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text, expanded]);

  return (
    <div className={cn("relative content-compact", className)} data-testid="entity-page-header-description">
      <p
        id={id}
        ref={ref}
        className={cn("content-compact text-default-secondary break-words", expanded ? "inline" : "line-clamp-2")}
        data-clamped={expanded ? "false" : "true"}
      >
        {text}
      </p>
      {(overflows || expanded) && (
        <button
          type="button"
          className={cn(
            "inline-flex h-[1lh] items-center rounded-sm content-compact font-medium text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
            // The 24 px hit area: transparent, centred on the one-line box.
            "before:absolute before:inset-x-0 before:top-1/2 before:h-6 before:-translate-y-1/2 before:content-['']",
            expanded ? "relative ml-1 align-baseline" : "absolute bottom-0 right-0 bg-elevation-default pl-1",
          )}
          data-placement={expanded ? "inline" : "line-end"}
          data-box="one-line"
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => setExpanded((v) => !v)}
        >
          {!expanded && (
            <span
              aria-hidden
              data-testid="entity-page-header-description-fade"
              className="pointer-events-none absolute inset-y-0 right-full w-8 bg-gradient-to-r from-transparent to-[var(--bg-elevation-default)]"
            />
          )}
          {expanded ? t_i18n("Show less") : t_i18n("Read more")}
        </button>
      )}
    </div>
  );
}

/** The header's outer box, the icon tile and the title type, shared with
 *  `EntityPageHeaderSkeleton` so the loading header is the loaded one. */
const HEADER_CLASS = "mb-4 space-y-1";
const TILE_CLASS = "flex h-14 w-14 shrink-0 items-center justify-center rounded-sm";
const TITLE_CLASS = "text-2xl font-bold";

/**
 * The entity header while the entity is read: the real back link (it is
 * known before the read), then the loaded header's own boxes with their
 * content greyed out: the 56 px tile, the title on its 32 px line, the
 * description lines and the meta row, the actions top right. Nothing moves
 * when the entity lands (#11345).
 */
export function EntityPageHeaderSkeleton({
  backTo,
  backLabel,
  tile = true,
  descriptionLines = 0,
  meta = true,
  actions = 0,
}: {
  backTo: string | number;
  /** An i18n key, translated by BackButton. */
  backLabel: string;
  /** The loaded header draws an icon tile. */
  tile?: boolean;
  /** Lines of description the loaded header usually shows (0 to 2). */
  descriptionLines?: 0 | 1 | 2;
  /** The loaded header draws a badge or meta row under the title. */
  meta?: boolean;
  /** How many header actions the loaded header shows (icon buttons). */
  actions?: number;
}) {
  return (
    <div className={HEADER_CLASS} data-testid="entity-page-header-skeleton">
      <BackButton to={backTo} label={backLabel} />
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 flex-1 items-start gap-4">
          {tile && (
            <div className={TILE_CLASS} data-slot="tile">
              <Skeleton className="size-full" />
            </div>
          )}
          <div className="min-w-0 flex-1 space-y-0.5">
            <div className={cn("flex items-center", TITLE_CLASS)} data-slot="title">
              {/* An empty line box of the h1's type keeps its 32 px height. */}
              <span aria-hidden>&#8203;</span>
              <Skeleton className="h-6 w-64" />
            </div>
            {Array.from({ length: descriptionLines }).map((_, i) => (
              <div key={i} className="flex items-center content-compact" data-slot="description">
                <span aria-hidden>&#8203;</span>
                <Skeleton className={cn("h-3", i === 0 ? "w-3/4" : "w-1/2")} />
              </div>
            ))}
            {meta && (
              /* A Badge is 24 px tall (its 18 px line, py-0.5 and its border): the meta row is too. */
              <div className="flex items-center gap-x-4" data-slot="meta">
                <Skeleton className="h-6 w-24" />
                <Skeleton className="h-6 w-16" />
              </div>
            )}
          </div>
        </div>
        {actions > 0 && (
          <div className="flex shrink-0 items-center gap-2" data-slot="actions">
            {Array.from({ length: actions }).map((_, i) => (
              <Skeleton key={i} className="size-8" />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function IconComponent({ icon: Icon, muted }: { icon: ElementType; muted?: boolean }) {
  return <Icon className={cn("h-7 w-7", muted ? "text-default-secondary" : "text-accent")} aria-hidden />;
}
