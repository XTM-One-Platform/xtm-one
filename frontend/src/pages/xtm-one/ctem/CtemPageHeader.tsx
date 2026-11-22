// SPDX-License-Identifier: Apache-2.0

/**
 * The one header band of the CTEM pages (#11660, design approved in UX round 2
 * of item 5f629f38): the command center, every stage page and every CTEM sub
 * page draw this band instead of a hero card or a free title block.
 *
 * The back link is the app's shared LocationBackButton (a `state.backTo`
 * of the opening page wins, as everywhere else), drawn first at the content
 * edge ABOVE the band and outside its border, exactly as on an entity page
 * (EntityPageHeader); there is no CTEM-only back link (Sam, 22 Nov 2026).
 *
 * One bordered band on the page grid, at most two rows:
 * - title block: a breadcrumb line (current page, page chips) over the title
 *   at heading size with its "i" (the methodology lives there, never as a
 *   paragraph in the header);
 * - stat cells: label above, value below, separated by hairlines that run
 *   the band's full content height, the cells centred on it, one figure per
 *   cell (a figure is never drawn twice);
 * - actions: right aligned on the title line;
 * - chips (the product pills of the command center): right aligned.
 *
 * The layout follows the band's own width, measured (a docked panel or the
 * sidebar narrowing the column counts as much as the viewport), see
 * `headerLayout`:
 * - wide (>= 135rem): one row, title | stats | actions | chips;
 * - medium (>= 90rem): title | stats | actions, the chips on a thin row
 *   under it, right aligned;
 * - narrow: the breadcrumb on a full row of its own, then title | actions
 *   (the actions on the h1 line), the stat cells inline (label, then value on
 *   one line) on a full row of their own, the chips on a thin row under them.
 *   The title column keeps the room of a greeting (UX round 2 on #605: in the
 *   title column the breadcrumb ran to 3 lines at 1080 and pushed the actions
 *   above the h1).
 * The thresholds hold the widest command center band (five cells, the
 * actions and four product pills) without a track painting over another (UX
 * round 1 on #11677, B1 and B2). Every element keeps one line
 * (`whitespace-nowrap`, truncation with the full text on hover).
 */
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { LocationBackButton } from "@/components/ui/back-button";
import { InfoTip } from "@/components/ui/info-tip";
import { useFormatter } from "@/i18n";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { cn } from "@/lib/utils";

/** One stat cell of the band: label above, value below (inline when narrow). */
export interface CtemHeaderStat {
  /** Stable id, used for the test id `ctem-header-stat-<id>`. */
  id: string;
  /** The cell's label, already translated. */
  label: string;
  /** The figure, drawn at stat size. */
  value: ReactNode;
  /** A short word or delta after the figure (band name, "-13"), already styled. */
  sub?: ReactNode;
  /** A swatch or ring drawn before the figure. */
  lead?: ReactNode;
  /** The full reading of the cell for assistive technology and on hover. */
  title?: string;
  /** Opens a page or a dialog: the whole cell becomes the button. */
  onOpen?: () => void;
}

export interface CtemPageHeaderProps {
  /** The page above this one, drawn as the shared BackButton above the band;
   *  omitted on the command center (the root). `label` is an i18n key. */
  back?: { to: string; label: string };
  /** The current page's name, first on the breadcrumb line. */
  crumb?: string;
  /** The first item of the breadcrumb line (the command center's eyebrow). */
  eyebrow?: ReactNode;
  /** Small chips and facts on the breadcrumb line (vertical, owner, edition). */
  crumbExtras?: ReactNode;
  title: string;
  /** The methodology or definition, behind the "i" beside the title. */
  hint?: ReactNode;
  /** Names the "i" for assistive technology. */
  hintLabel?: string;
  stats?: CtemHeaderStat[];
  actions?: ReactNode;
  /** Right-aligned chips (product pills). */
  chips?: ReactNode;
  className?: string;
  /** The height the band needs, so a dashboard grid sizes its cell to it
   *  (the command center draws the band as its first widget). */
  onContentHeight?: (px: number) => void;
}

export type CtemHeaderLayout = "narrow" | "medium" | "wide";

/** The band width (px) from which the stat cells sit beside the title (90rem). */
export const HEADER_MEDIUM_PX = 1440;
/** The band width (px) from which the chips join the title row (135rem). */
export const HEADER_WIDE_PX = 2160;

/** The layout of a band `widthPx` wide; not measured yet: narrow. */
export function headerLayout(widthPx: number | null): CtemHeaderLayout {
  if (widthPx == null) return "narrow";
  if (widthPx >= HEADER_WIDE_PX) return "wide";
  if (widthPx >= HEADER_MEDIUM_PX) return "medium";
  return "narrow";
}

/** The grid tracks and areas of a layout, only the areas the band draws. */
export function headerGrid(
  layout: CtemHeaderLayout,
  has: { stats: boolean; chips: boolean; crumb?: boolean },
): { columns: string; areas: string } {
  if (layout === "wide") {
    return { columns: "minmax(0,auto) minmax(0,1fr) auto auto", areas: "'title stats acts chips'" };
  }
  if (layout === "medium") {
    return {
      columns: "minmax(0,auto) minmax(0,1fr) auto",
      areas: ["'title stats acts'", has.chips && "'chips chips chips'"].filter(Boolean).join(" "),
    };
  }
  return {
    // The title keeps a greeting's room before the actions take theirs.
    columns: "minmax(min(14rem,100%),1fr) auto",
    areas: [has.crumb && "'crumb crumb'", "'title acts'", has.stats && "'stats stats'", has.chips && "'chips chips'"]
      .filter(Boolean)
      .join(" "),
  };
}

export function CtemPageHeader({
  back,
  crumb,
  eyebrow,
  crumbExtras,
  title,
  hint,
  hintLabel,
  stats,
  actions,
  chips,
  className,
  onContentHeight,
}: CtemPageHeaderProps) {
  const { t_i18n } = useFormatter();
  const hasChips = chips != null && chips !== false;
  const hasStats = (stats?.length ?? 0) > 0;
  // The band itself is measured, never a stretched parent: its width picks
  // the layout, its own height sizes the dashboard cell (UX round 1 on
  // #11677, B3: the cell stayed 380 px under a 65 px band).
  const ref = useRef<HTMLElement>(null);
  const [width, setWidth] = useState<number | null>(null);
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const report = () => {
      const box = node.getBoundingClientRect();
      if (box.width > 0) setWidth(Math.round(box.width));
      if (onContentHeight && box.height > 0) onContentHeight(Math.ceil(box.height));
    };
    report();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(report);
    observer.observe(node);
    return () => observer.disconnect();
  }, [onContentHeight]);
  const layout = headerLayout(width);
  const stacked = layout !== "narrow";
  const hasCrumb = Boolean(crumb || eyebrow || crumbExtras);
  // Narrow: the breadcrumb takes a full-width row of the band, so its line
  // wraps on the band's width and the actions sit on the h1 line.
  const crumbRow = hasCrumb && layout === "narrow";
  const grid = headerGrid(layout, { stats: hasStats, chips: hasChips, crumb: crumbRow });
  const breadcrumb = hasCrumb && (
    <nav
      aria-label={t_i18n("Breadcrumb")}
      style={crumbRow ? CRUMB_ROW_STYLE : BREADCRUMB_STYLE}
      className="flex min-w-0 items-center gap-x-1.5 gap-y-0.5 whitespace-nowrap content-caption text-default-secondary"
    >
      {eyebrow}
      {crumb && (
        // The current page keeps its words ("Stage 2 of 5" was cut at 1080,
        // A1 of round 2 on #11677): the chips after it give way.
        <span className="shrink-0 font-medium text-default-primary" aria-current="page" title={crumb}>
          {crumb}
        </span>
      )}
      {/* The extras are items of the breadcrumb line itself (the wrapper
          draws no box): the current page stays on line 1,
          an extra that does not fit goes whole to the next caption line,
          which starts at the left edge of the breadcrumb, never in a column
          beside the crumb (#604, UX round 1 on #605). An item wider than the
          whole line still truncates on its own. */}
      {crumbExtras && (
        <span style={CRUMB_EXTRAS_STYLE} data-testid="ctem-page-header-crumb-extras">
          {crumbExtras}
        </span>
      )}
    </nav>
  );
  return (
    <div data-testid="ctem-page-header" data-layout={layout} className={cn(back && HEADER_WITH_BACK_CLASS)}>
      {/* The app's one back link, as on an entity page: first, at the
          content edge, above the band and outside its border. */}
      {back && <LocationBackButton to={back.to} label={back.label} />}
      <header
        ref={ref}
        style={{ gridTemplateColumns: grid.columns, gridTemplateAreas: grid.areas }}
        className={cn(
          "grid items-center gap-x-5 gap-y-1.5 rounded-sm border border-elevation-subtle-soft bg-elevation-default px-5 py-2",
          className,
        )}
      >
        {crumbRow && breadcrumb}
        <div className="flex min-w-0 flex-col gap-0.5 [grid-area:title]" data-testid="ctem-page-header-title">
          {!crumbRow && breadcrumb}
          <div className="flex min-w-0 items-center gap-2">
            <h1 className="truncate font-sans-geologica text-xl font-bold leading-tight tracking-tight" title={title}>
              {title}
            </h1>
            {hint && <InfoTip content={hint} label={hintLabel ?? title} />}
          </div>
        </div>

        {hasStats && (
          <CtemStatRow
            stats={stats!}
            stacked={stacked}
            // The cells take the band's full content height: the hairlines
            // run top to bottom and each cell centres its figure on the same
            // axis as the title block and the actions.
            style={STAT_AREA_STYLE}
            className={cn("justify-self-start [grid-area:stats]", stacked && "border-l border-elevation-subtle-soft")}
          />
        )}

        {actions && (
          <div className="flex shrink-0 items-center gap-2 whitespace-nowrap justify-self-end [grid-area:acts]" data-testid="ctem-page-header-actions">
            {actions}
          </div>
        )}

        {hasChips && (
          <div className="flex min-w-0 items-center justify-end gap-1.5 justify-self-end [grid-area:chips]" data-testid="ctem-page-header-chips">
            {chips}
          </div>
        )}
      </header>
    </div>
  );
}

/**
 * The stat cells as one row that never runs past its box (production retest
 * 195b5f64 of #11677: the French and Spanish rows were 870 and 887 px in an
 * 810 px band at 1080, the last cell clipped and the page scrolling
 * sideways). The row wraps: a cell that does not fit goes to a second row,
 * whole, never clipped (`STAT_ROW_STYLE`, read by the test in every
 * language). A label longer than its cell truncates with the full text on
 * hover. Also the compact score row of the command center hero.
 */
export function CtemStatRow({ stats, stacked, className, style }: { stats: CtemHeaderStat[]; stacked: boolean; className?: string; style?: CSSProperties }) {
  const { t_i18n } = useFormatter();
  return (
    <div
      role="group"
      aria-label={t_i18n("Key figures")}
      style={{ ...STAT_ROW_STYLE, ...style }}
      className={cn("flex min-w-0 max-w-full items-stretch gap-y-1.5", className)}
      data-testid="ctem-page-header-stats"
    >
      {stats.map((s) => (
        <StatCell key={s.id} stat={s} stacked={stacked} />
      ))}
    </div>
  );
}

/** The wrapper's rhythm when the back link sits above the band: the spacing of EntityPageHeader (`space-y-1`). */
const HEADER_WITH_BACK_CLASS = "space-y-1";

/** The stat cells take the band's full content height, so their hairlines run top to bottom and their figures centre on the title's axis; read by the test. */
export const STAT_AREA_STYLE = { alignSelf: "stretch" } as const;

/** Wrap, never clip: the rule the no-overflow test pins. */
export const STAT_ROW_STYLE = { flexWrap: "wrap", overflow: "visible" } as const;

/** The breadcrumb line wraps whole items onto a second caption line, never clips them (#604); read by the test. */
export const BREADCRUMB_STYLE = { flexWrap: "wrap", minWidth: 0 } as const;

/** The extras wrapper draws no box: its items flow on the breadcrumb line (#604, UX round 1 on #605); read by the test. */
export const CRUMB_EXTRAS_STYLE = { display: "contents" } as const;

/** Narrow band: the breadcrumb on its own full-width grid row (UX round 2 on #605); read by the test. */
export const CRUMB_ROW_STYLE = { ...BREADCRUMB_STYLE, gridArea: "crumb" } as const;

function StatCell({ stat, stacked }: { stat: CtemHeaderStat; stacked: boolean }) {
  const body = (
    <>
      <span className="max-w-full truncate content-caption uppercase tracking-wider text-default-secondary" title={stat.label}>
        {stat.label}
      </span>
      <span className="flex items-center gap-1.5 tabular-nums">
        {stat.lead}
        <span className={cn("font-sans-geologica font-bold leading-tight", stacked ? "text-xl" : "text-lg")} data-testid={`ctem-header-stat-${stat.id}-value`}>
          {stat.value}
        </span>
        {stat.sub && <span className="content-caption font-medium">{stat.sub}</span>}
      </span>
    </>
  );
  // Narrow: label and value on one line; medium and wide: label above the value.
  const cell = cn(
    "flex min-w-0 max-w-full whitespace-nowrap border-r border-elevation-subtle-soft last:border-r-0",
    stacked ? "flex-col items-start justify-center gap-0.5 px-4" : "items-center gap-1.5 px-3 first:pl-0",
  );
  if (stat.onOpen) {
    return (
      <button
        type="button"
        onClick={stat.onOpen}
        title={stat.title}
        aria-label={stat.title}
        className={cn(cell, "cursor-pointer rounded-sm text-left hover:bg-elevation-hover transition-colors", BUTTON_ROLE_FOCUS_RING)}
        data-testid={`ctem-header-stat-${stat.id}`}
      >
        {body}
      </button>
    );
  }
  return (
    <div className={cell} title={stat.title} data-testid={`ctem-header-stat-${stat.id}`}>
      {body}
    </div>
  );
}
