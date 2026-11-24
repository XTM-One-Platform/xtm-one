// SPDX-License-Identifier: Apache-2.0

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { Icon, Tabs, TabsList, TabsTrigger, type IconName } from "@filigran/design-system";
import { cn, escapeRegExp } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { useCommandSections } from "@/lib/commands/CommandsProvider";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { dropLastTab, fitTabs, listOverflows, revealTabInBar } from "@/components/ui/detail-tabs-fit";

/**
 * The tab bar every detail page renders — now on the design system's `Tabs`.
 *
 * What the library takes over: the bar's appearance (this used to be a
 * hand-styled pill group) and its keyboard behaviour, which Radix's Tabs
 * provides and this did not — arrow keys move between tabs, Home/End jump to the
 * ends, and the roving tabindex means the bar is one tab stop rather than N.
 *
 * `activationMode="manual"` is deliberate. Radix selects on focus by default,
 * which for a bar made of router links would navigate on every arrow keypress.
 * Manual keeps arrows as "move focus" and Enter as "go".
 *
 * What stays here, because none of it is the library's business: deriving each
 * tab's href from the current path, publishing the tabs as the ⌘K palette's `@`
 * targets, and translating the labels.
 */

/**
 * The trigger spacing of every detail tab bar. The library's trigger is
 * `px-4 gap-2`: at 1024 px the six Operations tabs measured 802 px in a
 * 748 px list and "Workspaces" sat behind the scroll chevron. Below
 * `xl` the padding drops to 10 px and the icon gap to 6 px (730 px for those
 * six tabs); from `xl` the library's spacing is restored.
 */
const TAB_TRIGGER_CLASS = "whitespace-nowrap px-2.5 gap-1.5 xl:px-4 xl:gap-2";

/**
 * Width the "More tabs" menu takes beside the list: its 32 px trigger and the
 * 8 px gap before it (`gap-2` on the bar row).
 */
const MORE_TABS_WIDTH = 40;

interface DetailTabsProps<T extends string> {
  /**
   * `to`: the tab's own route, for a tab row spanning several pages (a nav
   * family, #9301 N3: Traces is a tab beside Analytics' Model usage). Omitted:
   * the current path's base plus the tab key, as for a detail page.
   */
  tabs: DetailTab<T>[];
  active: T;
  /** @deprecated Omit to use URL-based navigation (preferred). */
  onChange?: (tab: T) => void;
  /**
   * The query string (with its `?`, or empty) a tab link keeps from the
   * current URL, e.g. a list filter shared by every tab. Omitted:
   * links carry no query, as before.
   */
  searchFor?: (tab: T, current: URLSearchParams) => string;
  /** The tab list's accessible name, when the page holds more than one tab row. */
  ariaLabel?: string;
  /** Test id of the tab list. */
  testId?: string;
}

/**
 * One tab of the bar. `icon` is optional: a sub-page row such as the
 * organization profile tabs (#12329) has none. `trailing` is drawn after the
 * label inside the trigger (a count, an unsaved or error marker); it is not
 * repeated in the "More tabs" menu.
 */
export interface DetailTab<T extends string> {
  key: T;
  label: string;
  icon?: React.ElementType;
  /** A design system icon name, drawn by its `Icon` at 16 px (a nav family's
   *  members); wins over `icon`. */
  glyph?: IconName;
  to?: string;
  trailing?: React.ReactNode;
  testId?: string;
}

/** A tab's glyph: a design system name drawn by its `Icon` (16 px, its
 *  smallest size), else the component as it was. */
function tabGlyph(tab: Pick<DetailTab<string>, "icon" | "glyph">, className: string) {
  if (tab.glyph) return <Icon name={tab.glyph} size={16} />;
  const Glyph = tab.icon;
  return Glyph ? <Glyph className={className} aria-hidden /> : undefined;
}

/**
 * The tab bar while the page's entity is read: the library's own `TabsList`
 * and triggers (same height, padding and gaps as `DetailTabs`), each trigger
 * holding an icon and a label placeholder instead of its text. Hidden from
 * assistive technology and out of the tab order: nothing in it can be used.
 */
export function DetailTabsSkeleton({ count }: { count: number }) {
  return (
    <div className="flex min-w-0 flex-1 items-center gap-2" aria-hidden data-testid="detail-tabs-skeleton">
      <Tabs value="tab-0" activationMode="manual" className="min-w-0">
        <TabsList className="max-w-full overflow-x-auto">
          {Array.from({ length: count }).map((_, i) => (
            <TabsTrigger key={i} value={`tab-${i}`} disabled tabIndex={-1} className={TAB_TRIGGER_CLASS}>
              <Skeleton className="size-3.5 shrink-0" />
              <Skeleton className={cn("h-3", i % 2 === 0 ? "w-16" : "w-20")} />
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>
    </div>
  );
}

export function DetailTabs<T extends string>({ tabs, active, onChange, searchFor, ariaLabel, testId }: DetailTabsProps<T>) {
  // Labels are translated here (single source of truth for every detail
  // page tab bar) — already-translated or dynamic labels pass through.
  const { t_i18n } = useFormatter();
  const location = useLocation();
  const params = useParams<{ tab?: string }>();
  const defaultKey = tabs[0]?.key;

  // Base path = current path without the tab segment (and any sub-segments).
  // Uses a regex anchored at the end so nested sections like `/config/tooling`
  // are fully stripped, leaving `/agents/123` (not `/agents/123/config`).
  // `rawTab` is a user-controlled URL segment, so it MUST be regex-escaped —
  // an unescaped metacharacter (e.g. `/flows/foo(`) would make `new RegExp`
  // throw and crash the page.
  const rawTab = params.tab;
  const basePath = rawTab
    ? location.pathname.replace(new RegExp(`/${escapeRegExp(rawTab)}(/.*)?$`), "")
    : location.pathname;

  const hrefFor = (key: T) => {
    const own = tabs.find((tab) => tab.key === key)?.to;
    const path = own ?? (key === defaultKey ? basePath : `${basePath}/${key}`);
    return searchFor ? `${path}${searchFor(key, new URLSearchParams(location.search))}` : path;
  };

  /* Publish these tabs as the ⌘K palette's `@` targets. Done here rather than
     in each page: this component is what every tabbed screen already renders,
     and it is the only place that knows both the tab list and where each tab
     leads. Registration is additive, so a page with a nested section bar
     contributes both. */
  const navigate = useNavigate();
  useCommandSections(tabs.map(({ key, label }) => ({
    id: key,
    title: label,
    select: () => (onChange ? onChange(key) : navigate(hrefFor(key))),
  })));

  /* When the list scrolls (narrow widths), keep the active tab in view: a
     deep link to the last tab must not open with its own trigger off-screen.
     The library shows its scroll chevrons only after measuring, which narrows
     the list after the first paint, so the scroll is redone (instant, not
     smooth) whenever the list is resized. A tab reached by the keyboard
     (arrows, Home, End) is a link that moves focus without activating, so it
     is revealed on focus too. The reveal moves the list's own scrollLeft
     (`revealTabInBar`), never `scrollIntoView`: that also scrolls the window,
     and the ResizeObserver re-ran it after a deep link had scrolled to its
     anchor, sending the page back up to the tab bar. */
  const barRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const bar = barRef.current;
    const list = bar?.querySelector<HTMLElement>('[role="tablist"]');
    if (!bar || !list) return;
    const reveal = () => {
      const activeTab = list.querySelector<HTMLElement>('[role="tab"][data-state="active"]');
      if (activeTab) revealTabInBar(activeTab, bar);
    };
    reveal();
    const revealFocused = (event: FocusEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.getAttribute("role") === "tab") {
        revealTabInBar(target, bar);
      }
    };
    list.addEventListener("focusin", revealFocused);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(reveal);
    observer?.observe(list);
    return () => {
      list.removeEventListener("focusin", revealFocused);
      observer?.disconnect();
    };
  }, [active]);

  /* Tabs wider than the bar go into a "More tabs" menu: spacing alone
     cannot fit a long catalog (Spanish Operations needs 167 px more than the
     1024 px bar), and a tab behind the scroll chevron is a tab nobody finds.
     `kept` lists the keys left in the bar; `null` is the measuring pass, where
     every trigger is rendered so its width can be read. The pass runs in a
     layout effect, before paint, whenever the bar's width, the labels or the
     active tab change; the wrapper is `flex-1 min-w-0`, so its width never
     depends on the tabs it holds (but see the scrollbar below). The rule itself
     is `fitTabs` (the active tab always stays in the bar).

     The measuring pass renders the list at its natural width (`w-max`, not
     painted: the pass commits its result before paint), so the library never
     sees it overflow and never shows its scroll chevron for it: a chevron left
     from that pass narrowed the committed list by its own width and kept
     itself alive, so one tab too many went to the menu
     (B1 and B3). Once the kept set is committed the list is checked again
     (`listOverflows`, the library's own chevron test), and a list that still
     scrolls (sub-pixel widths, a late font) drops its last non-active tab
     into the menu until it no longer does. The kept set is the largest that
     fits without a chevron.

     The measuring pass must not move the page either: the natural-width list
     overflowed the bar, the page grew a scrollbar for that one pass, and with
     always-visible scrollbars the bar lost their width (1196 → 1190 px at
     1440 px). The key read during the pass then never matched the committed
     bar, every commit started a new pass, and React stopped the loop with
     "Maximum update depth exceeded", the whole page left on "Failed to load
     this section". So the bar clips what the pass lays out (`overflow-hidden`,
     only while measuring: a committed bar must not clip focus rings), and the
     key is read from the committed bar only, never during the pass: a width the
     pass itself causes can no longer start another one. */
  const [kept, setKept] = useState<T[] | null>(null);
  const [barWidth, setBarWidth] = useState(0);
  const [listWidth, setListWidth] = useState(0);
  const measuredFor = useRef<string | null>(null);
  const labelSignature = tabs.map(({ key, label }) => `${key}:${t_i18n(label)}`).join("|");
  useLayoutEffect(() => {
    const bar = barRef.current;
    const list = bar?.querySelector<HTMLElement>('[role="tablist"]');
    if (!bar || !list) return;
    if (kept !== null) {
      const measureKey = `${bar.clientWidth}|${labelSignature}|${active}`;
      // First commit after a pass: this bar is what the kept set is shown in.
      if (measuredFor.current === null) measuredFor.current = measureKey;
      if (measuredFor.current !== measureKey) {
        setKept(null);
      } else if (kept.length > 1 && listOverflows({ scrollWidth: list.scrollWidth, clientWidth: list.clientWidth })) {
        const activeIndex = tabs.findIndex((tab) => tab.key === active);
        const indices = tabs.flatMap((tab, index) => (kept.includes(tab.key) ? [index] : []));
        const next = dropLastTab(indices, activeIndex);
        if (next.length < kept.length) setKept(next.map((index) => tabs[index].key));
      }
      return;
    }
    const triggers = Array.from(list.querySelectorAll<HTMLElement>('[role="tab"]'));
    if (triggers.length !== tabs.length) return;
    const style = getComputedStyle(list);
    const px = (value: string) => Number.parseFloat(value) || 0;
    const next = fitTabs({
      widths: triggers.map((trigger) => trigger.getBoundingClientRect().width),
      gap: px(style.columnGap),
      chrome: px(style.paddingLeft) + px(style.paddingRight) + px(style.borderLeftWidth) + px(style.borderRightWidth),
      available: bar.clientWidth,
      more: MORE_TABS_WIDTH,
      activeIndex: tabs.findIndex((tab) => tab.key === active),
    }).map((index) => tabs[index].key);
    measuredFor.current = null;
    setKept(next);
    // `tabs` is a fresh array on most renders; `labelSignature` stands for it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kept, barWidth, listWidth, labelSignature, active]);
  useEffect(() => {
    const bar = barRef.current;
    const list = bar?.querySelector<HTMLElement>('[role="tablist"]');
    if (!bar || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      setBarWidth(bar.clientWidth);
      if (list) setListWidth(list.clientWidth);
    });
    observer.observe(bar);
    if (list) observer.observe(list);
    return () => observer.disconnect();
  }, []);

  /* A tab picked in the menu becomes the active tab, which changes the measure
     key: the measuring pass renders no menu, so the trigger that held focus
     unmounts and focus would fall to the page body. Focus moves to the newly
     active tab instead, once the bar is committed and after the menu's own
     close handling (which tries to return focus to its trigger). */
  const focusAfterPick = useRef<T | null>(null);
  useEffect(() => {
    if (kept === null || focusAfterPick.current === null || focusAfterPick.current !== active) return;
    const timer = setTimeout(() => {
      focusAfterPick.current = null;
      barRef.current?.querySelector<HTMLElement>('[role="tab"][data-state="active"]')?.focus();
    }, 0);
    return () => clearTimeout(timer);
  }, [kept, active]);
  const shown = kept === null ? tabs : tabs.filter((tab) => kept.includes(tab.key));
  const overflow = kept === null ? [] : tabs.filter((tab) => !kept.includes(tab.key));

  return (
    <div
      ref={barRef}
      className={cn("flex min-w-0 flex-1 items-center gap-2", kept === null && "overflow-hidden")}
      data-measuring={kept === null || undefined}
    >
    <Tabs
      value={active}
      activationMode="manual"
      onValueChange={onChange ? (value) => onChange(value as T) : undefined}
      className={kept === null ? "w-max shrink-0" : "min-w-0"}
    >
      {/* A label never wraps: below the bar's natural width a two-line label
          broke the bar. Below `xl` the triggers are tighter
          (`TAB_TRIGGER_CLASS`); the tabs that still do not fit go into the
          "More tabs" menu beside the list. The list keeps its
          horizontal scroll only for a bar too narrow for even one tab. */}
      <TabsList aria-label={ariaLabel} data-testid={testId} className={kept === null ? "overflow-x-auto" : "max-w-full overflow-x-auto"}>
        {shown.map(({ key, label, icon: TabIcon, glyph, trailing, testId: tabTestId }) => {
          /* Legacy: with `onChange`, the trigger is the control itself and the
             library renders its own icon slot. */
          if (onChange) {
            return (
              <TabsTrigger key={key} value={key} className={TAB_TRIGGER_CLASS} data-testid={tabTestId} icon={tabGlyph({ icon: TabIcon, glyph }, "size-3.5")}>
                {t_i18n(label)}
                {trailing}
              </TabsTrigger>
            );
          }

          /* Path-based: `asChild` onto a Link so tabs stay bookmarkable and
             right-clickable. The library cannot inject its icon slot around an
             arbitrary child in that mode, so the icon is composed here. The
             active link is the current page (`aria-current`). */
          return (
            <TabsTrigger key={key} value={key} className={TAB_TRIGGER_CLASS} data-testid={tabTestId} asChild>
              <Link to={hrefFor(key)} aria-current={key === active ? "page" : undefined}>
                {tabGlyph({ icon: TabIcon, glyph }, "size-3.5 shrink-0")}
                {t_i18n(label)}
                {trailing}
              </Link>
            </TabsTrigger>
          );
        })}
      </TabsList>
    </Tabs>
    {overflow.length > 0 && (
      <OverflowMenu
        label={t_i18n("More tabs")}
        className="shrink-0"
        items={overflow.map(({ key, label, icon, glyph }) => ({
          id: `tab-${key}`,
          label: t_i18n(label),
          icon,
          glyph,
          onSelect: () => {
            focusAfterPick.current = key;
            if (onChange) onChange(key);
            else navigate(hrefFor(key));
          },
        }))}
      />
    )}
    </div>
  );
}
