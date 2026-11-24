// SPDX-License-Identifier: Apache-2.0

/**
 * Left navigation — a product adapter over the design system's `Navbar`.
 *
 * The tree itself still lives in `nav-model.ts` (shared with the hub pages and
 * the ⌘K palette); this file maps it onto library components and owns nothing
 * about how a nav row looks.
 *
 * What moved to the library, and what that removed from here:
 *
 *  - Row geometry, hover/focus/selected states and the collapsed rail are
 *    `Navbar` + `NavbarItem`. The 36px row height, the 2px selected accent and
 *    the 180px/48px widths are its Figma contract, not constants here.
 *  - The rail's hover flyouts are `NavbarSubmenu`, which is a WAI-ARIA
 *    disclosure while expanded and a `role="menu"` flyout while collapsed —
 *    one component for both presentations. That deleted ~120 lines of
 *    hand-rolled flyout, its hover-intent handling and its Escape wiring.
 *  - The collapse toggle is rendered by `Navbar` itself, in its footer. It used
 *    to sit next to the logo; the library puts it at the bottom, and following
 *    it is the point of adopting the library.
 *
 * The shell is OpenCTI's and OpenAEV's: the product switcher on top, blocks
 * separated by a rule with no heading, "Made by Filigran" and the collapse
 * toggle at the foot. Anything else (About, XTM One Desktop) lives in the
 * profile menu.
 *
 * What stayed here, deliberately:
 *
 *  - Mobile. `Navbar` has no overlay presentation, so the off-canvas wrapper,
 *    its backdrop and the hamburger are still the host's.
 *  - `aria-current="page"`. Both `NavbarItem` and `NavbarSubmenuItem` derive
 *    their selected state from it rather than a prop, so routing stays the
 *    source of truth for what is active.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Lock, Menu, X } from "lucide-react";
import {
  Icon,
  Navbar,
  NavbarItem,
  NavbarSeparator,
  NavbarSubmenu,
  NavbarSubmenuItem,
  ProductSwitcher,
  useNavbarCollapsed,
  type ProductSwitcherOption,
} from "@filigran/design-system";
import { cn } from "@/lib/utils";
import { CTEM_PRODUCT_INFO } from "@/lib/constants";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { useFormatter } from "@/i18n";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useAuth } from "@/hooks/useAuth";
import { useColorMode } from "@/hooks/useColorMode";
import {
  useIsXtmOne, usePlatformConfig, usePlatformEmblemUrl, usePlatformLogoUrl, usePlatformName,
  useChatEnabled, useCustomVariablesEnabled, useAiEnabled, useAgenticAIEnabled,
} from "@/hooks/usePlatformConfig";
import { usePlatformRegistrations } from "@/hooks/api/usePlatformApi";
import { useXtmHubRegistrationStatus } from "@/hooks/api/useXtmHubApi";
import { useRelaysEnabled } from "@/hooks/api/useRelayApi";
import { useNavPrefs } from "@/hooks/useNavPrefs";
import { useAiDisabledReason } from "@/hooks/useAiDisabled";
import { useEnterpriseLockedReason } from "@/hooks/useEnterpriseUpsell";
import { EnterpriseLockedTip } from "@/components/ui/enterprise-upsell";
import openctiWordmark from "@/assets/products/opencti.svg";
import openctiWordmarkLight from "@/assets/products/opencti-light.svg";
import openaevWordmark from "@/assets/products/openaev.svg";
import openaevWordmarkLight from "@/assets/products/openaev-light.svg";
import xtmHubWordmark from "@/assets/products/xtm-hub.svg";
import xtmHubWordmarkLight from "@/assets/products/xtm-hub-light.svg";
import opencrqWordmark from "@/assets/products/opencrq.svg";
import opencrqWordmarkLight from "@/assets/products/opencrq-light.svg";
import xtmOneWordmark from "@/assets/products/xtm-one.svg";
import xtmOneWordmarkLight from "@/assets/products/xtm-one-light.svg";
import xtmOneMark from "@/assets/products/xtm-one-mark.svg";
import xtmOneMarkLight from "@/assets/products/xtm-one-mark-light.svg";
import { MadeByFiligran } from "./MadeByFiligran";
import { NavRowBody } from "./NavRowBody";
import {
  activeNavPath, isGroupActive, navLabel, resolveNav,
  type NavFlags, type NavGroup, type NavLeaf, type NavSection,
} from "./nav-model";
import { SUITE_PRODUCT_NAMES, productSwitcherEntries, type SuiteProduct, type SwitcherEntry } from "./product-switcher";

/**
 * The tooltip a locked entry explains itself with — picked by WHY it is
 * locked, because the administrator's switch and the missing licence are
 * different remedies. Passes children through untouched when not locked, so
 * every nav renderer can wrap unconditionally and none of them shows a lock
 * without its explanation.
 *
 * `wrap` stays false by default here: the controls this branch wraps are live
 * links that hover on their own, and the extra span distorts the row geometry
 * the library sets.
 */
function LockTip({
  entry, side = "right", wrap = false, children,
}: {
  entry: Pick<NavLeaf, "locked" | "lockReason">;
  side?: "top" | "right";
  wrap?: boolean;
  children: ReactNode;
}) {
  if (!entry.locked) return <>{children}</>;
  return entry.lockReason === "unlicensed" ? (
    <EnterpriseLockedTip side={side} wrap={wrap}>{children}</EnterpriseLockedTip>
  ) : (
    <AiDisabledTip side={side} wrap={wrap}>{children}</AiDisabledTip>
  );
}

/**
 * One navigation row.
 *
 * `asChild` is what makes the row a real anchor — middle-click, copy-link and
 * keyboard activation all come from the router `Link` rather than an onClick
 * handler. The library documents the trade: in `asChild` mode it cannot inject
 * anything around the child, so the row body is `NavRowBody`, and the rail's
 * tooltip needs its text as `tooltipLabel`.
 */
function NavRow({
  leaf, active, onNavigate,
}: {
  leaf: NavLeaf;
  active: boolean;
  onNavigate: () => void;
}) {
  const { t_i18n } = useFormatter();
  const collapsed = useNavbarCollapsed();
  const aiOffReason = useAiDisabledReason();
  const unlicensedReason = useEnterpriseLockedReason();
  const label = navLabel(leaf, t_i18n);
  const lockReason = leaf.locked
    ? (leaf.lockReason === "unlicensed" ? unlicensedReason : aiOffReason)
    : null;

  /* Dimmed, not disabled: the entry stays a working link because the read
     surface is the point of the mode (FR-017, SC-006). The rail has no room
     beside the icon, so the lock rides its corner there. */
  const lock = (className: string) => leaf.locked && (
    <Lock className={cn("shrink-0 text-feedback-warning-primary", className)} aria-hidden />
  );
  /* The rail's one tooltip says what the entry is, then why it is locked: a
     lock tooltip of its own would open on top of the library's. */
  const tooltip = lockReason ? (
    <span className="block space-y-1">
      <span className="block">{label}</span>
      <span className="block text-default-secondary">{lockReason}</span>
    </span>
  ) : label;

  const row = (
    <NavbarItem asChild tooltipLabel={tooltip}>
      <Link
        to={leaf.to}
        onClick={onNavigate}
        aria-current={active ? "page" : undefined}
      >
        <NavRowBody
          icon={<Icon name={leaf.icon} size={16} />}
          badge={collapsed && lock("absolute -bottom-1 -right-1.5 size-2.5")}
          trailing={lock("ml-auto size-3")}
        >
          {label}
        </NavRowBody>
      </Link>
    </NavbarItem>
  );
  if (collapsed) return row;
  /* wrap={false}: the row is a live link, so it hovers on its own — the extra
     span would only distort the row geometry. */
  return <LockTip entry={leaf}>{row}</LockTip>;
}

/**
 * The body of a row of a group or a collapsible section, inline or in a rail
 * flyout: dimmed and marked when locked, its reason in its accessible name and
 * its tooltip as on a top-level row (#9301 UX r3 A3), never removed (FR-017).
 * The body only: the library reads its submenu's children by type, so the
 * `NavbarSubmenuItem` and its link stay the direct child (`submenuRow`).
 *
 * No tooltip of our own here (#10292 UX r1 B1, B2): the library's row already
 * opens one on hover and on focus whenever a descendant is clipped, with the
 * row's text as its content, and the visually hidden reason is such a
 * descendant. A second trigger wrapped inside the link added a tab stop per
 * locked row and stacked a second tooltip; the row's text is therefore written
 * to read as one sentence, "Label: reason", for the tooltip and the screen
 * reader alike.
 *
 * Full colour when locked (#10292 UX r2 B2): the 12px label dimmed to 60%
 * fell to 2.95:1 in light and 3.86:1 in dark, under the 4.5:1 floor. The lock
 * icon carries the state, as on the top-level rows, which are not dimmed.
 */
function NavSubmenuRowBody({ leaf }: { leaf: NavLeaf }) {
  const { t_i18n } = useFormatter();
  const aiOffReason = useAiDisabledReason();
  const unlicensedReason = useEnterpriseLockedReason();
  const lockReason = leaf.locked
    ? (leaf.lockReason === "unlicensed" ? unlicensedReason : aiOffReason)
    : null;
  return (
    <span className="flex min-w-0 flex-1 items-center gap-2">
      {/* 16px as every row of the suite's navbars (DS NavbarItem), never 14px. */}
      <Icon name={leaf.icon} size={16} />
      <span className="truncate">{navLabel(leaf, t_i18n)}</span>
      {leaf.locked && (
        <>
          <Lock className="ml-auto size-3 shrink-0 text-feedback-warning-primary" aria-hidden />
          {lockReason && <span className="sr-only">{`: ${lockReason}`}</span>}
        </>
      )}
    </span>
  );
}

/** One row of a submenu: a real link, the library's item as the direct child. */
const submenuRow = (leaf: NavLeaf, activePath: string | null, onNavigate: () => void) => (
  <NavbarSubmenuItem key={leaf.to} asChild>
    <Link to={leaf.to} onClick={onNavigate} aria-current={activePath === leaf.to ? "page" : undefined}>
      <NavSubmenuRowBody leaf={leaf} />
    </Link>
  </NavbarSubmenuItem>
);

/** A group of leaves: the library's submenu, inline when expanded, flyout when not. */
function NavGroupBlock({
  group, activePath, open, onOpenChange, onNavigate,
}: {
  group: NavGroup;
  activePath: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onNavigate: () => void;
}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const collapsed = useNavbarCollapsed();
  const aiOffReason = useAiDisabledReason();
  const unlicensedReason = useEnterpriseLockedReason();
  const hub = group.to;
  const onHub = !!hub && activePath === hub;

  /* A group with a hub page opens it when its heading is clicked, and unfolds
     to show its pages beside it - what the hand-rolled sidebar did with a label
     link and a separate chevron. The library's expanded heading is ONE control,
     an accordion trigger with no link and no second button, so the same intent
     is read off its toggle:
       - from anywhere else, a click lands on the hub and leaves the group open
         (a click that would have folded an open group navigates instead);
       - on the hub itself, a click folds or unfolds the group, the one thing
         left for it to do.
     Keyboard activation goes through the same path. Collapsed, the library
     already makes the heading a link to `to` and opens its flyout on hover, so
     the open state passes straight through there. */
  const handleOpenChange = (next: boolean) => {
    if (collapsed || !hub) {
      onOpenChange(next);
      return;
    }
    if (!onHub) {
      navigate(hub);
      onNavigate();
      if (next) onOpenChange(true);
      return;
    }
    onOpenChange(next);
  };

  const lockReason = group.locked
    ? (group.lockReason === "unlicensed" ? unlicensedReason : aiOffReason)
    : null;

  return (
    <NavbarSubmenu
      label={
        /* Marked, never removed: same contract as the leaf rows (FR-017), and
           the same pattern (#10292 UX r2 B1). No tooltip wrapper of our own:
           it nested a focusable span inside the library's trigger button (two
           tab stops) and opened on keyboard focus only. The reason rides the
           heading's name as visually hidden text, so the library's own
           overflow tooltip shows "Label: reason" on hover and focus, and a
           cut label gets the same tooltip. Full colour, the lock icon carries
           the state (UX r2 B2, contrast). */
        <span className="flex min-w-0 items-center gap-2">
          {/* A label cut at the rail width ("Tools & Integrations" at 1440
              en, #9301) keeps its full text on hover through the library
              tooltip; no native title on top of it (#10338 UX r1 B1). */}
          <span className="truncate">{t_i18n(group.label)}</span>
          {group.locked && (
            <>
              <Lock className="size-3 shrink-0 text-feedback-warning-primary" aria-hidden />
              {lockReason && <span className="sr-only">{`: ${lockReason}`}</span>}
            </>
          )}
        </span>
      }
      icon={<Icon name={group.icon} size={16} />}
      open={open}
      onOpenChange={handleOpenChange}
      // The heading reads as selected on its hub page, as the label link did;
      // elsewhere the library marks it from an active child as usual.
      active={onHub ? true : undefined}
      // Collapsed, the heading itself is the link to the hub (the library
      // ignores `to` while expanded, where `onOpenChange` above takes over).
      to={hub}
      linkComponent={Link}
    >
      {group.children.map((child) => submenuRow(child, activePath, onNavigate))}
    </NavbarSubmenu>
  );
}

/**
 * A suite product's logo in the switcher, the row's only visible content.
 *
 * The wordmark files are the suite's one switcher set, byte-identical to the
 * files OpenCTI, OpenAEV and OpenCRQ draw their own switchers with (the
 * library declares its product wordmarks without exporting them, F09): full
 * #0FBCFF on dark, full #0015A8 on light, 164 units wide. Every row is the
 * product's wordmark alone, OpenCRQ's included.
 */
const SUITE_WORDMARKS: Record<SuiteProduct, { dark: string; light: string }> = {
  opencti: { dark: openctiWordmark, light: openctiWordmarkLight },
  openaev: { dark: openaevWordmark, light: openaevWordmarkLight },
  opencrq: { dark: opencrqWordmark, light: opencrqWordmarkLight },
  xtm_hub: { dark: xtmHubWordmark, light: xtmHubWordmarkLight },
};

/**
 * XTM One's own lockup in the header and the switcher, emblem and wordmark in
 * the emblem's one purple: #A47AF0 (purple-300, the Figma emblem colour) on
 * dark, #651FE5 (the `.light` --color-filigran-ia-primary, purple-500) on
 * light. An exception for product logos and switchers only (Sam, 21 Nov 2026,
 * #964). Every other surface draws the bundled public/ files, the Figma
 * lockup (wordmark #F2F2F3 dark, #18191B light).
 */
const XTM_ONE_HEADER = {
  logo: { dark: xtmOneWordmark, light: xtmOneWordmarkLight },
  mark: { dark: xtmOneMark, light: xtmOneMarkLight },
} as const;

function suiteLogo(product: SuiteProduct, mode: "light" | "dark") {
  return <img src={SUITE_WORDMARKS[product][mode]} alt="" className="h-auto w-full object-contain" />;
}

/**
 * Copilot mode's header: the platform logo, a link home, and nothing else. The
 * suite switcher is XTM One's (Sam, 21 Nov 2026): Copilot is not a suite
 * product, so it has no chevron and no menu. The slots are the library
 * switcher's own (28px tall, 126px wide expanded, a 28px square in the rail),
 * so the logo sits exactly where XTM One's does.
 */
function PlatformHomeLogo({ logo, mark, label }: { logo: ReactNode; mark: ReactNode; label: string }) {
  const collapsed = useNavbarCollapsed();
  return (
    <Link
      to="/"
      aria-label={label}
      className={cn(
        "inline-flex shrink-0 items-center focus-visible:outline-none focus-visible:ring-2 ring-focus",
        collapsed ? "size-7 justify-center" : "h-7 w-[126px] overflow-hidden",
      )}
    >
      {collapsed ? mark : logo}
    </Link>
  );
}

export function Sidebar({ impersonating = false }: { impersonating?: boolean }) {
  const { pathname } = useLocation();
  /* The mobile drawer is open on the page it was opened on: any navigation,
     the logo's included, closes it. */
  const [mobileOpenOn, setMobileOpenOn] = useState<string | null>(null);
  const mobileOpen = mobileOpenOn === pathname;
  const setMobileOpen = (open: boolean) => setMobileOpenOn(open ? pathname : null);
  const { t_i18n } = useFormatter();
  const { isAdmin, isGroupManager } = useAuth();
  const platformConfig = usePlatformConfig();
  const isXtm = useIsXtmOne();
  const platformName = usePlatformName();
  const logoSrc = usePlatformLogoUrl();
  const emblemSrc = usePlatformEmblemUrl();
  const chatEnabled = useChatEnabled();
  const customVariablesEnabled = useCustomVariablesEnabled();
  const relaysEnabled = useRelaysEnabled() === true;
  const aiEnabled = useAiEnabled();
  // Only lock on a RESOLVED "not licensed": the hook fails closed while the
  // config is in flight, and a lock that flashes on every load of a licensed
  // install teaches users to ignore it.
  const aiLicensed = useAgenticAIEnabled() || !platformConfig;
  const deepInvestigationEnabled = useFeatureFlag("DEEP_INVESTIGATION");
  const videoStudioEnabled = useFeatureFlag("VIDEO_STUDIO");
  const dashboardStudioEnabled = useFeatureFlag("DASHBOARD_STUDIO");
  const evaluationOn = useFeatureFlag("EVALUATION");
  const { rail, setRail, isExpanded, setGroupExpanded } = useNavPrefs();
  const { data: registrations } = usePlatformRegistrations();
  const { data: hubRegistration } = useXtmHubRegistrationStatus();
  const colorMode = useColorMode();

  /* The rail is a desktop affordance: the mobile sidebar is an overlay with
     room for labels, so opening it always shows the full nav. */
  const collapsed = rail && !mobileOpen;

  /* The rail's flyouts are a hover surface with a state of their own, never the
     persisted expansion, which holds the current page's group open and would
     open its flyout on load. Set from the value the library sends, never
     toggled: it reports "open" again when the pointer crosses into the panel. */
  const [railFlyout, setRailFlyout] = useState<string | null>(null);
  const submenuState = (id: string) => (collapsed
    ? {
      open: railFlyout === id,
      onOpenChange: (next: boolean) => setRailFlyout((current) => (next ? id : current === id ? null : current)),
    }
    : { open: isExpanded(id), onOpenChange: (next: boolean) => setGroupExpanded(id, next) });

  const flags = useMemo<NavFlags>(() => ({
    chat: chatEnabled,
    customVariables: customVariablesEnabled,
    relays: relaysEnabled,
    xtmOne: isXtm,
    aiAdoption: platformConfig?.features.ai_adoption !== false,
    aiEnabled,
    aiLicensed,
    deepInvestigation: deepInvestigationEnabled,
    health: true,
    videoStudio: videoStudioEnabled,
    dashboardStudio: dashboardStudioEnabled,
    evaluation: evaluationOn,
    /* Intents are XTM One domain content: no row in Copilot mode (#445). */
    intents: isXtm,
    /* The CTEM cases list is XTM One domain content too: no row in Copilot mode (#1135). */
    ctemCases: isXtm,
  }), [chatEnabled, customVariablesEnabled, relaysEnabled, isXtm, platformConfig?.features.ai_adoption, aiEnabled, aiLicensed, deepInvestigationEnabled, videoStudioEnabled, dashboardStudioEnabled, evaluationOn]);

  const sections = useMemo(() => resolveNav(flags, isAdmin, isGroupManager), [flags, isAdmin, isGroupManager]);
  const activePath = useMemo(() => activeNavPath(sections, pathname), [sections, pathname]);

  /* Open the group that owns the page being navigated to — then leave it under
     user control (forcing it open would make its chevron look broken). */
  useEffect(() => {
    if (!activePath) return;
    for (const section of sections) {
      if (section.collapsible && section.entries.some((e) => e.kind === "leaf" && e.to === activePath)) {
        setGroupExpanded(section.id, true);
      }
      for (const entry of section.entries) {
        if (entry.kind === "group" && isGroupActive(entry, activePath)) {
          setGroupExpanded(entry.id, true);
        }
      }
    }
  }, [activePath, sections, setGroupExpanded]);

  const closeMobile = () => setMobileOpen(false);
  const onNavigate = () => closeMobile();

  /**
   * The suite products, connected or not (`productSwitcherEntries`). `tooltip`
   * says where the row leads, the use the library documents for it; a
   * platform's own title joins it when it is not the product's name, so two
   * instances of one product are told apart.
   */
  const productOptions = useMemo<ProductSwitcherOption[]>(() => {
    const tooltip = (entry: SwitcherEntry) => {
      if (entry.target === "public") return t_i18n("Get {product} now", { product: entry.label });
      if (entry.target === "register") return t_i18n("Register on the XTM Hub");
      const status = entry.target === "connected" ? t_i18n("Connected") : t_i18n("No recent heartbeat");
      return entry.label === SUITE_PRODUCT_NAMES[entry.product] ? status : `${entry.label} - ${status}`;
    };
    return productSwitcherEntries({
      registrations,
      hubRegistration,
      publicPages: { opencti: CTEM_PRODUCT_INFO.opencti.productUrl, openaev: CTEM_PRODUCT_INFO.openaev.productUrl },
      // The registration lives in Settings > Platform, an XTM One section.
      hubRegistrationPath: isAdmin && isXtm ? "/settings/platform" : null,
    }).map((entry) => ({
      id: entry.id,
      label: entry.label,
      logo: suiteLogo(entry.product, colorMode),
      tooltip: tooltip(entry),
      ...(entry.href ? { href: entry.href } : { to: entry.to }),
    }));
  }, [registrations, hubRegistration, isAdmin, isXtm, colorMode, t_i18n]);

  /* The bundled logo is in use while the backend names its light variant; an
     operator's own logo and mark are drawn as uploaded. */
  const bundledXtmOne = isXtm && Boolean(platformConfig?.platform_logo_light);
  const headerLogoSrc = bundledXtmOne ? XTM_ONE_HEADER.logo[colorMode] : logoSrc;
  const headerMarkSrc = bundledXtmOne && platformConfig?.platform_emblem_light ? XTM_ONE_HEADER.mark[colorMode] : emblemSrc;

  const brandLogo = (
    <img src={headerLogoSrc} alt={platformName} className="h-7 min-w-0 object-contain" />
  );
  /* The rail is 48px wide: a wordmark squeezed into its 28px square is an
     unreadable strip, so the rail shows the mark alone. With no mark to show
     (an operator's logo and no favicon), the logo is cropped to its leading
     square, which is where a wordmark carries its mark. */
  const brandMark = headerMarkSrc ? (
    <img src={headerMarkSrc} alt="" className="size-7 object-contain" />
  ) : (
    <img src={logoSrc} alt="" className="size-7 max-w-none object-cover object-left" />
  );

  const navbarHeader = (
    <>
      {isXtm ? (
        <ProductSwitcher
          logo={brandLogo}
          logoCollapsed={brandMark}
          label={t_i18n("Switch product")}
          options={productOptions}
          logoTo="/"
          linkComponent={Link}
          logoLabel={`${platformName} home`}
        />
      ) : (
        <PlatformHomeLogo logo={brandLogo} mark={brandMark} label={`${platformName} home`} />
      )}
      {/* Mobile only: the backdrop closes the nav too, but a visible control is
          the discoverable way out on touch. */}
      <button
        type="button"
        className="shrink-0 p-1 text-default-secondary hover:text-default-primary lg:hidden"
        onClick={closeMobile}
        aria-label={t_i18n("Close navigation")}
      >
        <X className="size-5" />
      </button>
    </>
  );

  const renderSection = (section: NavSection, index: number) => {
    /* A collapsible section IS a submenu: one label, one icon, a list of
       leaves. Before the library it needed a bespoke rail flyout to stay
       reachable when collapsed; the submenu handles both presentations. */
    if (section.collapsible && section.icon) {
      const leaves = section.entries.filter((e): e is NavLeaf => e.kind === "leaf");
      return (
        <div key={section.id}>
          {index > 0 && <NavbarSeparator />}
          <NavbarSubmenu
            label={section.label ? t_i18n(section.label) : section.id}
            icon={<Icon name={section.icon} size={16} />}
            {...submenuState(section.id)}
          >
            {leaves.map((leaf) => submenuRow(leaf, activePath, onNavigate))}
          </NavbarSubmenu>
        </div>
      );
    }

    return (
      <div key={section.id}>
        {index > 0 && <NavbarSeparator />}
        {section.entries.map((entry) => (entry.kind === "leaf" ? (
          <NavRow
            key={entry.to}
            leaf={entry}
            active={activePath === entry.to}
            onNavigate={onNavigate}
          />
        ) : (
          <NavGroupBlock
            key={entry.id}
            group={entry}
            activePath={activePath}
            {...submenuState(entry.id)}
            onNavigate={onNavigate}
          />
        )))}
      </div>
    );
  };

  return (
    <>
      {/* Mobile hamburger — hidden when the nav is open */}
      {!mobileOpen && (
        <button
          type="button"
          /* top-3 centres a 36px button on the 68px bar. Hard-coding the
             arithmetic is what drifted last time the bar's height changed, so
             it reads the same custom property the header does. */
          style={{ top: "calc((var(--fds-header-height, 68px) - 2.25rem) / 2)" }}
          className="fixed left-4 z-50 flex size-9 items-center justify-center rounded-sm layer-1 bg-elevation-default lg:hidden"
          onClick={() => setMobileOpen(true)}
          aria-label={t_i18n("Open navigation")}
        >
          <Menu className="size-5" />
        </button>
      )}

      {/* Mobile overlay */}
      {mobileOpen && (
        <div className="fixed inset-0 z-40 bg-scrim lg:hidden" onClick={closeMobile} />
      )}

      {/* Positioning wrapper. The library's Navbar carries no position of its
          own — it owns its width and its internals — so the off-canvas
          transform, the z-index and the impersonation-banner offset stay here. */}
      <div
        className={cn(
          "fixed left-0 z-40 flex transition-transform duration-300 lg:translate-x-0",
          impersonating ? "top-9 h-[calc(100vh-2.25rem)]" : "top-0 h-screen",
          mobileOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <Navbar
          aria-label={t_i18n("Main navigation")}
          collapsed={collapsed}
          onCollapsedChange={setRail}
          header={navbarHeader}
          footer={<MadeByFiligran />}
          className="h-full"
        >
          {sections.map(renderSection)}
        </Navbar>
      </div>
    </>
  );
}
