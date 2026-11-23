/**
 * Integration catalog browser — the single presentation of the provider
 * catalog, wherever the catalog is browsed.
 *
 * Amazon-style faceted browsing: a sticky left sidebar with checkbox facets
 * (categories grouped by cybersecurity-first domains, connection modes,
 * availability), a search + sort toolbar, and category-sectioned provider
 * cards. All filtering is client-side over the provider catalog.
 *
 * Two surfaces share it, and they differ only in what a card *means*:
 *  - `browse` (/integrations) — a card is a provider you install, or one you
 *    already run. Licence gates apply and a provider an admin switched off is
 *    inert, shown under a "disabled by your administrator" banner.
 *  - `admin` (/settings/integrations) — a card opens that provider's details,
 *    so nothing is gated and a disabled provider stays clickable: switching it
 *    back on is the whole point of the surface.
 */

import { useCallback, useMemo, useState, type ReactNode } from "react";
import {
  CheckCircle2, ListFilter, Lock, Plug2, Plus, RotateCcw, Sparkles, X, Zap,
} from "lucide-react";
import { IntegrationStatusRibbon } from "@/components/ui/integration-status-ribbon";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SearchInput } from "@/components/ui/search-input";
import { TruncatedText } from "@/components/ui/truncated-text";
import { FacetCheckbox, FacetGroupLabel, FacetSidebar } from "@/components/ui/facet-sidebar";
import {
  Select, SelectTrigger, SelectContent, SelectItem, SelectValue,
} from "@/components/ui/select";
import { AgenticGateDialog, EEBadge, ChatGateDialog } from "@/components/ui/license-gate-dialog";
import { cn, toggleArrayItem } from "@/lib/utils";
import {
  INTEGRATION_CATEGORY_GROUP_LABELS,
  INTEGRATION_CATEGORY_GROUPS,
  INTEGRATION_CATEGORY_ICONS,
  INTEGRATION_TYPE_LABELS,
  integrationCategoryLabel,
} from "@/lib/constants";
import {
  AUTH_FACET_LABELS,
  AUTH_FACET_ORDER,
  authModesOf,
  isReadyToConnect,
} from "./catalog-facets";
import { INTEGRATION_STATUS } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import { useCompactListToolbar } from "@/hooks/useCompactToolbar";
import { useAgenticAIEnabled, useChatSendEnabled } from "@/hooks/usePlatformConfig";
import type { ProviderCatalogItem } from "@/types";

// ── Facet definitions ────────────────────────────────────────────────────────

type SortMode = "name" | "ready" | "installed";

/**
 * What the browsing surface knows about a provider beyond its catalog entry:
 * how many integrations the viewer already runs on it, and the status those
 * integrations are in.
 *
 * Computed by the page, not here — resolving "which status wins when three
 * Jira integrations disagree" is a question about integrations, and this
 * component only knows about the catalog.
 */
export interface ProviderInstallState {
  count: number;
  /** An `INTEGRATION_STATUS` key: what the card's badge and border say. */
  status: string;
}

/** The install facet labels, catalog keys read through `t_i18n`. @i18n-keys */
const INSTALL_FACET_LABELS = { installed: "Installed", available: "Not installed" } as const;
const INSTALL_FACETS = [
  { id: "installed", label: INSTALL_FACET_LABELS.installed },
  { id: "available", label: INSTALL_FACET_LABELS.available },
] as const;
type InstallFacet = (typeof INSTALL_FACETS)[number]["id"];

/**
 * The pseudo-category the installed providers are gathered under.
 *
 * "Installed first" has to mean first on the PAGE, not first inside whichever
 * category section a provider happens to sit in — an installed Grafana five
 * sections down is not "first" by any reading the viewer would recognise. So
 * under that sort they leave the taxonomy and head their own section.
 */
const INSTALLED_SECTION = "__installed__";

/** The availability facet labels, catalog keys read through `t_i18n`. @i18n-keys */
const AVAILABILITY_FACET_LABELS = { ready: "Ready to connect", events: "Real-time events" } as const;
const AVAILABILITY_FACETS = [
  { id: "ready", label: AVAILABILITY_FACET_LABELS.ready },
  { id: "events", label: AVAILABILITY_FACET_LABELS.events },
] as const;
type AvailabilityFacet = (typeof AVAILABILITY_FACETS)[number]["id"];

function matchesAvailability(item: ProviderCatalogItem, facet: AvailabilityFacet): boolean {
  if (facet === "ready") return isReadyToConnect(item);
  return item.available_events.length > 0;
}

interface Filters {
  search: string;
  categories: string[];
  auth: string[];
  availability: AvailabilityFacet[];
  install: InstallFacet[];
}

type FacetGroup = "categories" | "auth" | "availability" | "install";

function matchesFilters(
  item: ProviderCatalogItem,
  f: Filters,
  installed: (item: ProviderCatalogItem) => boolean,
  skip?: FacetGroup,
): boolean {
  if (f.search) {
    const q = f.search.toLowerCase();
    const haystack = [
      item.name,
      item.provider,
      item.description ?? "",
      integrationCategoryLabel(item.integration_category),
      ...item.integration_types.map((t) => INTEGRATION_TYPE_LABELS[t] ?? t),
    ].join(" ").toLowerCase();
    if (!haystack.includes(q)) return false;
  }
  if (skip !== "categories" && f.categories.length > 0 && !f.categories.includes(item.integration_category)) return false;
  if (skip !== "auth" && f.auth.length > 0 && !authModesOf(item).some((m) => f.auth.includes(m))) return false;
  if (skip !== "availability" && f.availability.length > 0 && !f.availability.some((a) => matchesAvailability(item, a))) return false;
  if (skip !== "install" && f.install.length > 0) {
    const is = installed(item);
    if (!f.install.some((i) => (i === "installed" ? is : !is))) return false;
  }
  return true;
}

// ── Provider card ────────────────────────────────────────────────────────────

/**
 * Which gate a provider answers to, in the one place that decides it.
 *
 * A2A follows agentic chat (the deployment tier); every other provider follows
 * the Agentic AI licence. The two disagree in both directions, so a surface
 * that asks the wrong one names a reason that is not true. Derived from
 * `a2a_directions` because only A2A carries it — if a non-A2A provider ever
 * becomes chat-gated, this is the line to change.
 */
function isChatGated(item: ProviderCatalogItem): boolean {
  return Boolean(item.a2a_directions?.length);
}

/** Whether THIS provider is currently out of reach, by its own gate. */
function useProviderGated(item: ProviderCatalogItem): boolean {
  const agenticEnabled = useAgenticAIEnabled();
  const chatEnabled = useChatSendEnabled();
  return isChatGated(item) ? !chatEnabled : !agenticEnabled;
}

/**
 * Card contents, shared by every card variant.
 *
 * Owns the card's padding AND the positioning context of the absolutely
 * positioned status ribbon: the ribbon anchors to this box's padding
 * edge, which is the card corner, whatever the card around it establishes.
 * The header keeps the ribbon's diagonal band clear (`pr-16`): the band
 * (144 px, rotated 45deg in a 96 px corner box) crosses the name's top line
 * about 65 px in from the card's right edge (48 px inside the 16 px
 * padding), exactly where `pr-12` ended the header; 64 px keeps a long
 * name's top-right corner about 11 px clear of it at every card width
 * (tests_e2e/integration-status-ribbon.spec.ts).
 */
function ProviderCardBody({
  item, showGate, install,
}: {
  item: ProviderCatalogItem;
  showGate: boolean;
  install?: ProviderInstallState;
}) {
  const { t_i18n } = useFormatter();
  const capabilities = item.integration_types.slice(0, 3);
  const gated = useProviderGated(item);
  const statusCfg = install ? INTEGRATION_STATUS[install.status] ?? INTEGRATION_STATUS.disconnected : null;
  const StatusIcon = statusCfg?.icon;
  return (
    <div data-testid="provider-card-body" className="relative flex flex-1 flex-col p-4">
      <IntegrationStatusRibbon item={item} />
      <div className="mb-3 flex items-start gap-3 pr-16">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm border border-elevation-subtle-soft bg-elevation-hover transition-colors group-hover:border-accent/25">
          {item.logo
            ? <img src={item.logo} alt="" className="h-6 w-6 object-contain" />
            : <Plug2 className="h-5 w-5 text-default-secondary" />}
        </div>
        <div className="min-w-0 flex-1">
          <TruncatedText text={item.name} className="content-base font-semibold" />
          {item.is_custom && (
            <Badge variant="outline" className="mt-1 border-feedback-alert-primary/40 content-compact text-feedback-warning-primary">
              {t_i18n("Custom")}
            </Badge>
          )}
          {install && statusCfg && StatusIcon ? (
            <div className="mt-1 flex flex-wrap items-center gap-1">
              <Badge variant={statusCfg.badge} className="content-compact">
                <StatusIcon className="mr-0.5 h-2.5 w-2.5" />
                {t_i18n(statusCfg.label)}
              </Badge>
              {install.count > 1 && (
                <Badge variant="secondary" className="content-compact">
                  {t_i18n("{count} integrations", { count: install.count })}
                </Badge>
              )}
            </div>
          ) : isReadyToConnect(item) ? (
            <Badge variant="success" className="mt-1 content-compact">
              <CheckCircle2 className="mr-0.5 h-2.5 w-2.5" />{t_i18n("Ready to connect")}
            </Badge>
          ) : (
            <p className="mt-0.5 content-compact text-default-secondary">
              {item.auth_type in AUTH_FACET_LABELS ? t_i18n(AUTH_FACET_LABELS[item.auth_type]) : item.auth_type}
            </p>
          )}
        </div>
        {showGate && <EEBadge unavailable={gated} />}
      </div>
      <p className="mb-3 line-clamp-2 min-h-8 content-compact leading-relaxed text-default-secondary">
        {item.description || t_i18n(integrationCategoryLabel(item.integration_category))}
      </p>
      <div className="mt-auto flex flex-wrap items-center gap-1">
        {capabilities.map((t) => (
          <Badge key={t} variant="outline" className="border-elevation-subtle-soft content-compact font-normal text-default-secondary">
            {t_i18n(INTEGRATION_TYPE_LABELS[t] ?? t)}
          </Badge>
        ))}
        {item.available_events.length > 0 && (
          <span className="ml-auto inline-flex items-center gap-1 content-compact text-accent/80">
            <Zap className="h-2.5 w-2.5" />{t_i18n("Events")}
          </span>
        )}
      </div>
    </div>
  );
}

/**
 * A provider an admin has switched off platform-wide, on a surface that can
 * only consume the catalog.
 *
 * Shown rather than hidden so the catalog stays legible ("we do support this,
 * ask your admin") — dimmed under a wash, inert, and captioned with why.
 */
function DisabledProviderCard({ item }: { item: ProviderCatalogItem }) {
  const { t_i18n } = useFormatter();
  const notice = t_i18n("Disabled by your administrator");
  return (
    <div
      data-testid="provider-card-disabled"
      data-provider={item.provider}
      aria-disabled="true"
      className="relative flex w-full cursor-not-allowed select-none flex-col overflow-hidden rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default"
    >
      <div className="pointer-events-none flex flex-1 flex-col text-left opacity-45 grayscale">
        <ProviderCardBody item={item} showGate />
      </div>
      <div className="pointer-events-none absolute inset-0 bg-[color-mix(in_srgb,var(--bg-elevation-default)_30%,transparent)]" />
      <div className="relative flex items-center justify-center gap-1.5 border-t border-feedback-alert-primary/25 bg-feedback-alert-primary/[0.08] px-3 py-1.5 content-compact font-medium text-feedback-warning-tertiary">
        <Lock className="h-3 w-3 shrink-0" />
        {notice}
      </div>
    </div>
  );
}

/**
 * A provider on the browsing surface, installed or not.
 *
 * An installed provider is the one thing on this page the viewer already
 * owns, so it carries the emphasis — a status-coloured border and its status
 * badge. What it deliberately does NOT get is the dimmed, grayscale wash: on
 * this surface that treatment already means "disabled by your administrator",
 * an inert card. A provider you have yet to install is the opposite — it is
 * precisely the card you are meant to click — so it stays an ordinary tile.
 */
function BrowseProviderCard({
  item, onSelect, onAdd, install,
}: {
  item: ProviderCatalogItem;
  onSelect: (item: ProviderCatalogItem) => void;
  onAdd: (item: ProviderCatalogItem) => void;
  install?: ProviderInstallState;
}) {
  const { t_i18n } = useFormatter();
  if (item.is_disabled) return <DisabledProviderCard item={item} />;

  const tile = (
    <div
      data-testid="provider-card"
      data-provider={item.provider}
      data-installed={install ? "true" : "false"}
      className={cn(
        "group relative flex w-full flex-col rounded-sm border layer-1 bg-elevation-default backdrop-blur-sm transition-all duration-300",
        "hover:-translate-y-0.5 hover:border-accent/30 hover:shadow-[0_0_30px_color-mix(in_srgb,_var(--color-accent)_calc(0.12_*_100%),_transparent)]",
        install?.status === "connected" ? "border-feedback-success-primary/30 bg-feedback-success-primary/[0.03]"
          : install ? "border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.03]"
          : "border-elevation-subtle-soft",
      )}
    >
      {/* One button, over the card body. A control nested inside another
          control is flattened or skipped by assistive technology, so the add
          action below is a sibling rather than a button inside a button. */}
      <button
        type="button"
        className="flex flex-1 flex-col rounded-sm text-left focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
        onClick={() => onSelect(item)}
      >
        <ProviderCardBody item={item} showGate install={install} />
      </button>
      {install && (
        /* The card opens what the viewer already runs, so adding a second
           integration on the same provider needs its own way in. */
        <div className="flex justify-end border-t border-elevation-subtle-soft px-4 py-2">
          <Button
            variant="ghost"
            size="sm"
            className="h-6 gap-1 px-2 content-compact text-default-secondary"
            onClick={() => onAdd(item)}
          >
            <Plus className="h-3 w-3" /> {t_i18n("Add another")}
          </Button>
        </div>
      )}
    </div>
  );

  // Every tile shows; each asks its own gate — see `isChatGated`.
  const feature = t_i18n("Enabling the {name} integration", { name: item.name });
  if (isChatGated(item)) {
    return <ChatGateDialog feature={feature}>{tile}</ChatGateDialog>;
  }
  return (
    <AgenticGateDialog feature={feature} requiresAi={false}>
      {tile}
    </AgenticGateDialog>
  );
}

/**
 * The administration card: same body, plus whatever controls the surface hands
 * down.
 *
 * Structured like the browse card and for the same reason — the footer holds a
 * switch and a delete button, and nesting them inside the card's own button
 * (or inside a single `role="button"` widget) hides them from assistive
 * technology. So the body is the button, and the controls sit beside it.
 */
function AdminProviderCard({
  item, onSelect, footer,
}: {
  item: ProviderCatalogItem;
  onSelect: (item: ProviderCatalogItem) => void;
  footer?: (item: ProviderCatalogItem) => ReactNode;
}) {
  return (
    <div
      data-testid="catalog-provider-card"
      data-provider={item.provider}
      className={cn(
        "group relative flex w-full flex-col rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-sm transition-all duration-300",
        "hover:-translate-y-0.5 hover:border-accent/30 hover:shadow-[0_0_30px_color-mix(in_srgb,_var(--color-accent)_calc(0.12_*_100%),_transparent)]",
        item.is_disabled && "opacity-60",
      )}
    >
      <button
        type="button"
        className="flex flex-1 flex-col rounded-sm text-left focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
        onClick={() => onSelect(item)}
      >
        <ProviderCardBody item={item} showGate={false} />
      </button>
      {footer && (
        <div className="flex items-center gap-2 border-t border-elevation-subtle-soft px-4 py-3">
          {footer(item)}
        </div>
      )}
    </div>
  );
}

// ── Browser ──────────────────────────────────────────────────────────────────

interface IntegrationCatalogBrowserProps {
  catalog: ProviderCatalogItem[];
  /** What a card click means on this surface: pick a provider, or open it. */
  onSelect: (item: ProviderCatalogItem) => void;
  /** `browse` gates and inerts; `admin` opens everything. */
  variant?: "browse" | "admin";
  /** What the viewer already runs on a provider — `browse` only. Providers it
   *  answers for are emphasised, sorted first, and gain a Status facet. */
  installState?: (item: ProviderCatalogItem) => ProviderInstallState | undefined;
  /** "Add another" on an installed card — creating a second integration on a
   *  provider whose card otherwise opens the one the viewer has. */
  onAdd?: (item: ProviderCatalogItem) => void;
  /** Rendered above the hero stats — the surface's own heading and controls. */
  header?: ReactNode;
  /** Hero copy. Omit for the default catalog wording. */
  heroTitle?: ReactNode;
  heroDescription?: ReactNode;
  /** Controls pinned to the hero's top-right (import, bulk actions). */
  heroActions?: ReactNode;
  /** Callout under the hero stats — a warning the surface needs to make. */
  heroNotice?: ReactNode;
  /** Per-card controls, `admin` only (a switch, a delete button). */
  cardFooter?: (item: ProviderCatalogItem) => ReactNode;
  searchPlaceholder?: string;
}

export function IntegrationCatalogBrowser({
  catalog,
  onSelect,
  variant = "browse",
  installState,
  onAdd,
  header,
  heroTitle,
  heroDescription,
  heroActions,
  heroNotice,
  cardFooter,
  searchPlaceholder,
}: IntegrationCatalogBrowserProps) {
  const { t_i18n } = useFormatter();
  const [search, setSearch] = useState("");
  const [categories, setCategories] = useState<string[]>([]);
  const [auth, setAuth] = useState<string[]>([]);
  const [availability, setAvailability] = useState<AvailabilityFacet[]>([]);
  const [install, setInstall] = useState<InstallFacet[]>([]);
  // What the viewer already runs comes first by default — the page opens on
  // their own integrations, with the rest of the catalog below.
  const [sort, setSort] = useState<SortMode>(installState ? "installed" : "name");
  const compactToolbar = useCompactListToolbar();

  const isInstalled = useCallback(
    (item: ProviderCatalogItem) => Boolean(installState?.(item)),
    [installState],
  );

  const filters: Filters = useMemo(
    () => ({ search, categories, auth, availability, install }),
    [search, categories, auth, availability, install],
  );
  const hasFilters = search !== "" || categories.length > 0 || auth.length > 0
    || availability.length > 0 || install.length > 0;

  // Category groups actually represented in the catalog, with unknown
  // categories (future additions) collected into a trailing "Other" group.
  const categoryGroups = useMemo(() => {
    const known = new Set(INTEGRATION_CATEGORY_GROUPS.flatMap((g) => g.categories));
    const present = new Set(catalog.map((c) => c.integration_category));
    const groups = INTEGRATION_CATEGORY_GROUPS
      .map((g) => ({ ...g, categories: g.categories.filter((c) => present.has(c)) }))
      .filter((g) => g.categories.length > 0);
    const other = [...present].filter((c) => !known.has(c)).sort();
    if (other.length > 0) groups.push({ id: "other", label: INTEGRATION_CATEGORY_GROUP_LABELS.other, categories: other });
    return groups;
  }, [catalog]);

  // Facet counts follow standard faceted-search semantics: each facet group is
  // counted against the items filtered by everything *except* itself.
  const categoryCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of catalog) {
      if (matchesFilters(item, filters, isInstalled, "categories")) {
        counts[item.integration_category] = (counts[item.integration_category] ?? 0) + 1;
      }
    }
    return counts;
  }, [catalog, filters, isInstalled]);

  const authCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of catalog) {
      if (matchesFilters(item, filters, isInstalled, "auth")) {
        for (const mode of authModesOf(item)) counts[mode] = (counts[mode] ?? 0) + 1;
      }
    }
    return counts;
  }, [catalog, filters, isInstalled]);

  const availabilityCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of catalog) {
      if (matchesFilters(item, filters, isInstalled, "availability")) {
        for (const facet of AVAILABILITY_FACETS) {
          if (matchesAvailability(item, facet.id)) counts[facet.id] = (counts[facet.id] ?? 0) + 1;
        }
      }
    }
    return counts;
  }, [catalog, filters, isInstalled]);

  const installCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of catalog) {
      if (matchesFilters(item, filters, isInstalled, "install")) {
        const key = isInstalled(item) ? "installed" : "available";
        counts[key] = (counts[key] ?? 0) + 1;
      }
    }
    return counts;
  }, [catalog, filters, isInstalled]);

  const filtered = useMemo(
    () => catalog.filter((item) => matchesFilters(item, filters, isInstalled)),
    [catalog, filters, isInstalled],
  );

  // Results sectioned by category, in taxonomy order.
  const sections = useMemo(() => {
    const sortItems = (items: ProviderCatalogItem[]) => [...items].sort((a, b) => {
      // Providers nobody can connect sink to the bottom of their section,
      // whatever the sort mode.
      if (a.is_disabled !== b.is_disabled) return a.is_disabled ? 1 : -1;
      if (sort === "installed" && isInstalled(a) !== isInstalled(b)) {
        return isInstalled(a) ? -1 : 1;
      }
      if (sort === "ready" && a.has_oauth_defaults !== b.has_oauth_defaults) {
        return a.has_oauth_defaults ? -1 : 1;
      }
      return a.name.localeCompare(b.name);
    });
    const ordered = categoryGroups.flatMap((g) => g.categories);
    const lead = sort === "installed" && installState
      ? sortItems(filtered.filter(isInstalled))
      : [];
    const rest = lead.length > 0 ? filtered.filter((i) => !isInstalled(i)) : filtered;
    return [
      ...(lead.length > 0 ? [{ category: INSTALLED_SECTION, items: lead }] : []),
      ...ordered
        .map((cat) => ({ category: cat, items: sortItems(rest.filter((i) => i.integration_category === cat)) }))
        .filter((s) => s.items.length > 0),
    ];
  }, [filtered, categoryGroups, sort, isInstalled, installState]);

  const readyCount = catalog.filter(isReadyToConnect).length;
  const disabledCount = catalog.filter((c) => c.is_disabled).length;
  const categoryCount = categoryGroups.reduce((n, g) => n + g.categories.length, 0);
  const authOptions = AUTH_FACET_ORDER.filter((m) => catalog.some((c) => authModesOf(c).includes(m)));

  const installedCount = installState ? catalog.filter(isInstalled).length : 0;

  const clearAll = () => {
    setSearch("");
    setCategories([]);
    setAuth([]);
    setAvailability([]);
    setInstall([]);
  };

  const activeChips = [
    ...categories.map((c) => ({
      key: `cat-${c}`,
      label: t_i18n(integrationCategoryLabel(c)),
      remove: () => setCategories((prev) => prev.filter((x) => x !== c)),
    })),
    ...auth.map((m) => ({
      key: `auth-${m}`,
      label: t_i18n(AUTH_FACET_LABELS[m]),
      remove: () => setAuth((prev) => prev.filter((x) => x !== m)),
    })),
    ...availability.map((a) => ({
      key: `avail-${a}`,
      label: t_i18n(AVAILABILITY_FACETS.find((f) => f.id === a)?.label ?? a),
      remove: () => setAvailability((prev) => prev.filter((x) => x !== a)),
    })),
    ...install.map((i) => ({
      key: `install-${i}`,
      label: t_i18n(INSTALL_FACETS.find((f) => f.id === i)?.label ?? i),
      remove: () => setInstall((prev) => prev.filter((x) => x !== i)),
    })),
  ];

  return (
    <div className="space-y-6">
      {header}

      {/* Hero */}
      <div className="relative overflow-hidden rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-6 backdrop-blur-sm">
        <div className="pointer-events-none absolute -right-20 -top-24 h-64 w-64 rounded-full bg-accent/10 blur-3xl" />
        <div className="pointer-events-none absolute -bottom-32 left-1/3 h-56 w-56 rounded-full bg-accent/5 blur-3xl" />
        <div className="relative">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <h1 className="font-sans-geologica text-2xl font-bold">
                {heroTitle ?? (
                  <>Integration <span className="text-gradient-accent">Catalog</span></>
                )}
              </h1>
              <p className="mt-1.5 max-w-2xl content-base text-default-secondary">
                {heroDescription
                  ?? t_i18n("Connect the services your agents act on. All credentials are stored encrypted in the database.")}
              </p>
            </div>
            {heroActions && <div className="shrink-0">{heroActions}</div>}
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1.5 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2.5 py-1 content-compact text-default-secondary">
              <Plug2 className="h-3.5 w-3.5 text-accent" />
              <span className="font-medium tabular-nums text-default-primary">{catalog.length}</span>
              {t_i18n("{count, plural, one {integration} other {integrations}}", { count: catalog.length })}
            </span>
            <span className="inline-flex items-center gap-1.5 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2.5 py-1 content-compact text-default-secondary">
              <ListFilter className="h-3.5 w-3.5 text-accent" />
              <span className="font-medium tabular-nums text-default-primary">{categoryCount}</span>
              {t_i18n("{count, plural, one {category} other {categories}}", { count: categoryCount })}
            </span>
            {installedCount > 0 && (
              <span className="inline-flex items-center gap-1.5 rounded-sm border border-feedback-success-primary/25 bg-feedback-success-primary/[0.08] px-2.5 py-1 content-compact text-feedback-success-primary/90">
                <CheckCircle2 className="h-3.5 w-3.5" />
                <span className="font-medium tabular-nums">{installedCount}</span>
                {t_i18n("installed")}
              </span>
            )}
            {readyCount > 0 && (
              <span className="inline-flex items-center gap-1.5 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-2.5 py-1 content-compact text-default-secondary">
                <Sparkles className="h-3.5 w-3.5 text-feedback-success-primary" />
                <span className="font-medium tabular-nums text-default-primary">{readyCount}</span>
                {t_i18n("ready to connect")}
              </span>
            )}
            {disabledCount > 0 && (
              <span className="inline-flex items-center gap-1.5 rounded-sm border border-feedback-alert-primary/25 bg-feedback-alert-primary/[0.08] px-2.5 py-1 content-compact text-feedback-warning-tertiary">
                <Lock className="h-3.5 w-3.5" />
                {/* One plural message per variant, count included: gluing a
                    number onto a fragment has no plural form and assumes
                    English word order. */}
                <span className="tabular-nums">
                  {variant === "admin"
                    ? t_i18n("{count, plural, one {# integration disabled} other {# integrations disabled}}", { count: disabledCount })
                    : t_i18n(
                        "{count, plural, one {# integration disabled by your administrator} other {# integrations disabled by your administrator}}",
                        { count: disabledCount },
                      )}
                </span>
              </span>
            )}
          </div>
          {heroNotice && <div className="mt-4">{heroNotice}</div>}
        </div>
      </div>

      <div className="flex flex-col gap-6 lg:flex-row lg:items-start">
        {/* Faceted sidebar: the shared component the CTEM asset list draws too (#1501). */}
        <FacetSidebar hasFilters={hasFilters} onClearAll={clearAll}>
            {installState && (
              <div className="space-y-1">
                <FacetGroupLabel text={t_i18n("Status")} />
                <div className="space-y-px">
                  {INSTALL_FACETS.map((facet) => (
                    <FacetCheckbox
                      key={facet.id}
                      checked={install.includes(facet.id)}
                      count={installCounts[facet.id] ?? 0}
                      label={t_i18n(facet.label)}
                      onToggle={() => setInstall((prev) => toggleArrayItem(prev, facet.id))}
                    />
                  ))}
                </div>
              </div>
            )}

            {categoryGroups.map((group) => (
              <div key={group.id} className="space-y-1">
                <FacetGroupLabel text={t_i18n(group.label)} />
                <div className="space-y-px">
                  {group.categories.map((cat) => (
                    <FacetCheckbox
                      key={cat}
                      checked={categories.includes(cat)}
                      count={categoryCounts[cat] ?? 0}
                      icon={INTEGRATION_CATEGORY_ICONS[cat]}
                      label={t_i18n(integrationCategoryLabel(cat))}
                      onToggle={() => setCategories((prev) => toggleArrayItem(prev, cat))}
                    />
                  ))}
                </div>
              </div>
            ))}

            <div className="space-y-1 border-t border-elevation-subtle-soft pt-4">
              <FacetGroupLabel text={t_i18n("Connection")} />
              <div className="space-y-px">
                {authOptions.map((mode) => (
                  <FacetCheckbox
                    key={mode}
                    checked={auth.includes(mode)}
                    count={authCounts[mode] ?? 0}
                    label={t_i18n(AUTH_FACET_LABELS[mode])}
                    onToggle={() => setAuth((prev) => toggleArrayItem(prev, mode))}
                  />
                ))}
              </div>
            </div>

            <div className="space-y-1 border-t border-elevation-subtle-soft pt-4">
              <FacetGroupLabel text={t_i18n("Availability")} />
              <div className="space-y-px">
                {AVAILABILITY_FACETS.map((facet) => (
                  <FacetCheckbox
                    key={facet.id}
                    checked={availability.includes(facet.id)}
                    count={availabilityCounts[facet.id] ?? 0}
                    label={t_i18n(facet.label)}
                    onToggle={() => setAvailability((prev) => toggleArrayItem(prev, facet.id))}
                  />
                ))}
              </div>
            </div>
        </FacetSidebar>

        {/* Results */}
        <div className="min-w-0 flex-1 space-y-4">
          {/* Below 1280 the row never wraps: the search gives its width back down to
              its 128 px floor first, so Sort and the count stay on the one 32 px row
              at 1024 (UX r1 B1 on #10207); list-toolbar-compact.test.ts keeps it. */}
          <div
            data-testid="catalog-toolbar"
            className={cn("flex items-center gap-3", compactToolbar ? "flex-nowrap" : "flex-wrap")}
          >
            <SearchInput
              toolbar
              compact={compactToolbar}
              className="max-w-none"
              placeholder={searchPlaceholder ?? t_i18n("Search the catalog...")}
              value={search}
              onChange={setSearch}
            />
            <Select value={sort} onValueChange={(v) => setSort(v as SortMode)}>
              <SelectTrigger className="h-8 w-auto min-w-44 shrink-0" aria-label={t_i18n("Sort")}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {installState && <SelectItem value="installed">{t_i18n("Installed first")}</SelectItem>}
                <SelectItem value="name">{t_i18n("Name (A-Z)")}</SelectItem>
                <SelectItem value="ready">{t_i18n("Ready first")}</SelectItem>
              </SelectContent>
            </Select>
            <span className="shrink-0 rounded-sm bg-elevation-hover px-2.5 py-1 content-compact font-medium tabular-nums text-default-secondary">
              {t_i18n("{count, plural, one {# result} other {# results}}", { count: filtered.length })}
            </span>
          </div>

          {activeChips.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              {activeChips.map((chip) => (
                <button
                  key={chip.key}
                  type="button"
                  aria-label={t_i18n("Remove filter {name}", { name: chip.label })}
                  className="group inline-flex items-center gap-1 rounded-sm border border-accent/30 bg-accent/10 px-2 py-0.5 content-compact text-accent transition-colors hover:bg-accent/15"
                  onClick={chip.remove}
                >
                  {chip.label}
                  <X className="h-3 w-3 opacity-60 transition-opacity group-hover:opacity-100" />
                </button>
              ))}
              <Button variant="ghost" size="sm" className="h-6 px-2 content-compact text-default-secondary" onClick={clearAll}>
                {t_i18n("Clear all")}
              </Button>
            </div>
          )}

          {sections.length === 0 ? (
            /* An empty catalog and an over-filtered one are different
               problems: offering to reset filters that found nothing because
               there is nothing to find sends the reader after a fix that does
               not exist. */
            <div className="flex flex-col items-center justify-center rounded-sm border border-dashed border-elevation-subtle-soft py-16 text-center">
              <Plug2 className="mb-3 h-10 w-10 text-default-secondary" />
              {catalog.length === 0 ? (
                <p className="font-sans-geologica text-base font-semibold">{t_i18n("No providers in the catalog.")}</p>
              ) : (
                <>
                  <p className="font-sans-geologica text-base font-semibold">{t_i18n("No integrations match your filters")}</p>
                  <p className="mt-1 max-w-sm content-base text-default-secondary">
                    {t_i18n("Try a different search term or remove some filters to browse the full catalog.")}
                  </p>
                  <Button className="mt-4 gap-1.5" variant="secondary" onClick={clearAll}>
                    <RotateCcw className="h-3.5 w-3.5" /> {t_i18n("Reset filters")}
                  </Button>
                </>
              )}
            </div>
          ) : (
            <div className="space-y-8">
              {sections.map(({ category, items }) => {
                const installedSection = category === INSTALLED_SECTION;
                const Icon = installedSection
                  ? CheckCircle2
                  : INTEGRATION_CATEGORY_ICONS[category] ?? Plug2;
                return (
                  <section key={category} className="animate-fade-in">
                    <div className="mb-3 flex items-center gap-2.5">
                      <div className={cn(
                        "flex h-7 w-7 items-center justify-center rounded-sm border",
                        installedSection
                          ? "border-feedback-success-primary/25 bg-feedback-success-primary/10"
                          : "border-accent/20 bg-accent/10",
                      )}>
                        <Icon className={cn("h-3.5 w-3.5", installedSection ? "text-feedback-success-primary" : "text-accent")} />
                      </div>
                      <h2 className="font-sans-geologica content-base font-semibold">
                        {installedSection ? t_i18n("Installed") : t_i18n(integrationCategoryLabel(category))}
                      </h2>
                      <span className="rounded-sm bg-elevation-hover px-1.5 py-px content-compact font-medium tabular-nums text-default-secondary">
                        {items.length}
                      </span>
                      <div className="h-px flex-1 bg-elevation-hover" />
                    </div>
                    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
                      {items.map((item) => (
                        variant === "admin"
                          ? <AdminProviderCard key={item.provider} item={item} onSelect={onSelect} footer={cardFooter} />
                          : <BrowseProviderCard key={item.provider} item={item} onSelect={onSelect} onAdd={onAdd ?? onSelect} install={installState?.(item)} />
                      ))}
                    </div>
                  </section>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
