// SPDX-License-Identifier: Apache-2.0

/**
 * Hub page body shared by the nav groups that own one (`/work`,
 * `/agent-studio`, `/tools-and-integrations`).
 *
 * A hub exists to be *more* than a second copy of the menu — otherwise the
 * parent destination is a ceremonial extra click. So each card carries what the
 * sidebar cannot: what the resource is for, how many you have, when it last
 * changed, and a direct create action. Underneath, "Recently modified" merges
 * every resource type in the group so an interrupted piece of work is one click
 * away.
 */
import { Link } from "react-router-dom";
import { ArrowRight, Lock, Plus } from "lucide-react";
import { Icon, type IconName } from "@filigran/design-system";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { OverflowMenu } from "@/components/ui/overflow-menu";
import { AgenticGateDialog, AgenticEEBadgeForButton } from "@/components/ui/license-gate-dialog";
import { cn, pluralize, timeAgo } from "@/lib/utils";
import { useFormatter } from "@/i18n";

/** Minimal shape every hub resource list shares. */
export interface HubItem {
  id: string;
  name: string;
  updated_at: string;
}

export interface HubResource {
  /** List page — the whole card links here. */
  to: string;
  /** Create route. Omitted = no quick-create button on the card. */
  createTo?: string;
  /** Wrap the create button in the Agentic AI gate, as the list pages do. */
  gatedCreate?: boolean;
  /** Whether creating it needs AI. Defaults to true — declare the exemption. */
  requiresAi?: boolean;
  /** The design system glyph of the sidebar entry the card stands for. */
  icon: IconName;
  /** English label, translated here (matches the sidebar entry). */
  label: string;
  /** One line on what the resource is for. */
  description: string;
  items: HubItem[] | undefined;
  isLoading?: boolean;
  /**
   * The size of the whole set when `items` is only a recent WINDOW over it
   * (server-paginated lists such as runs and files). Defaults to
   * `items.length`, which is right for the lists fetched whole.
   */
  count?: number;
  /**
   * Detail route for a single item, used by "Recently modified". Omitted =
   * the item links to the list page (e.g. variables have no detail page).
   */
  detailTo?: (item: HubItem) => string;
  /** Set from the nav entry the card stands for (`hubCards`): the title carries
   *  the reason as a badge and the create action is hidden, as the menu row
   *  is locked (FR-017: listed and explained, never dropped). */
  locked?: boolean;
  lockReason?: "ai_disabled" | "unlicensed";
  /** The noun the count reads in, singular then plural (English, translated
   *  here). Defaults to item / items. */
  unit?: [string, string];
  /** Several ways to create, offered as a menu behind the one Create button,
   *  in the same labels and order as the list page header (Fleets). */
  createMenu?: { label: string; onSelect: () => void; disabled?: boolean }[];
  /** False when the card's items are another card's (Agentic Flow counts the
   *  agents): "Recently modified" then lists each object once. Default true. */
  inRecent?: boolean;
}

/** The lock badge of a hub card or feature title, in the menu's words. */
function LockBadge({ reason }: { reason: "ai_disabled" | "unlicensed" }) {
  const { t_i18n } = useFormatter();
  return (
    <Badge variant="outline" className="content-compact gap-1 font-normal">
      <Lock className="h-3 w-3" aria-hidden />
      {reason === "unlicensed" ? t_i18n("Unlicensed") : t_i18n("AI off")}
    </Badge>
  );
}

/* Timestamps are parsed rather than string-compared: the entries below are
   merged from several endpoints, and one serializing `+00:00` where another
   writes `Z` would otherwise sort wrong. */
const stamp = (iso: string) => new Date(iso).getTime();

function newestUpdate(items: HubItem[] | undefined): string | null {
  if (!items || items.length === 0) return null;
  return items.reduce(
    (latest, item) => (stamp(item.updated_at) > stamp(latest) ? item.updated_at : latest),
    items[0].updated_at,
  );
}

function ResourceCard({ resource }: { resource: HubResource }) {
  const { t_i18n } = useFormatter();
  const count = resource.count ?? resource.items?.length ?? 0;
  /* A locked card fetches nothing: no count and no date, never a false 0. */
  const lastUpdate = resource.locked ? null : newestUpdate(resource.items);

  return (
    <Card className="h-full border-elevation-subtle transition-colors hover:bg-elevation-hover">
      <CardContent className="flex h-full flex-col p-4">
        <Link to={resource.to} className="group flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
            <Icon name={resource.icon} size={20} className="text-accent" />
          </div>
          <div className="min-w-0 flex-1">
            <p className="flex items-center gap-1.5 content-base font-medium">
              {t_i18n(resource.label)}
              {resource.locked && resource.lockReason && <LockBadge reason={resource.lockReason} />}
              <ArrowRight className="h-3 w-3 opacity-0 transition-opacity group-hover:opacity-60" />
            </p>
            <p className="mt-0.5 content-compact leading-relaxed text-default-secondary">
              {t_i18n(resource.description)}
            </p>
          </div>
        </Link>

        <div className="mt-auto flex items-center gap-3 pt-4">
          <div className="min-w-0">
            {!resource.locked && (
              <Link to={resource.to} className="content-compact tabular-nums text-default-secondary hover:text-default-primary">
                {resource.isLoading
                  ? t_i18n("Loading…")
                  : `${count} ${t_i18n(pluralize(count, resource.unit?.[0] ?? "item", resource.unit?.[1]))}`}
              </Link>
            )}
            {lastUpdate && (
              <p className="truncate content-compact text-default-secondary">
                {t_i18n("Updated")} {timeAgo(lastUpdate)}
              </p>
            )}
          </div>
          {!resource.locked && resource.createMenu && (
            <div className="ml-auto shrink-0">
              <OverflowMenu
                label={t_i18n("Create")}
                trigger={<><Plus className="h-3.5 w-3.5" aria-hidden /> {t_i18n("Create")}</>}
                priority="secondary"
                size="sm"
                className="h-7"
                items={resource.createMenu.map((choice) => ({
                  id: choice.label,
                  label: t_i18n(choice.label),
                  onSelect: choice.onSelect,
                  disabled: choice.disabled,
                }))}
              />
            </div>
          )}
          {!resource.locked && !resource.createMenu && resource.createTo && (
            <div className="ml-auto shrink-0">
              {/* The gate's own copy is English, so the feature name stays the
                  untranslated label — as on the list pages ("Creating agents"). */}
              {resource.gatedCreate ? (
                <AgenticGateDialog
                  feature={`Creating ${resource.label}`}
                  requiresAi={resource.requiresAi ?? true}
                >
                  <Button size="sm" variant="outline" className="h-7 gap-1.5" asChild>
                    <Link to={resource.createTo}>
                      <Plus className="h-3.5 w-3.5" /> {t_i18n("Create")}
                      <AgenticEEBadgeForButton />
                    </Link>
                  </Button>
                </AgenticGateDialog>
              ) : (
                <Button size="sm" variant="outline" className="h-7 gap-1.5" asChild>
                  <Link to={resource.createTo}>
                    <Plus className="h-3.5 w-3.5" /> {t_i18n("Create")}
                  </Link>
                </Button>
              )}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

/** A featured call-to-action card, rendered first when a hub declares one. */
interface HubFeature {
  to: string;
  icon: IconName;
  label: string;
  description: string;
  gated?: boolean;
  /** As `HubResource.locked`: the badge says why, the gate dialog explains. */
  locked?: boolean;
  lockReason?: "ai_disabled" | "unlicensed";
}

function FeatureCard({ feature }: { feature: HubFeature }) {
  const { t_i18n } = useFormatter();
  const card = (
    <Link to={feature.to} className="block h-full">
      <Card className="h-full border-accent/30 bg-accent/[0.04] transition-colors hover:bg-accent/[0.08]">
        <CardContent className="flex h-full items-center gap-3 p-4">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/15">
            <Icon name={feature.icon} size={20} className="text-accent" />
          </div>
          <div className="min-w-0 flex-1">
            <p className="flex items-center gap-1.5 content-base font-medium">
              {t_i18n(feature.label)}
              {feature.locked && feature.lockReason && <LockBadge reason={feature.lockReason} />}
            </p>
            <p className="mt-0.5 content-compact leading-relaxed text-default-secondary">{t_i18n(feature.description)}</p>
          </div>
          <ArrowRight className="h-4 w-4 shrink-0 text-accent/70" />
        </CardContent>
      </Card>
    </Link>
  );
  return feature.gated
    ? <AgenticGateDialog feature={feature.label}>{card}</AgenticGateDialog>
    : card;
}

interface RecentEntry {
  key: string;
  name: string;
  updated_at: string;
  to: string;
  typeLabel: string;
  icon: IconName;
}

function RecentlyModified({ resources }: { resources: HubResource[] }) {
  const { t_i18n } = useFormatter();

  const entries: RecentEntry[] = resources
    .filter((resource) => resource.inRecent !== false && !resource.locked)
    .flatMap((resource) => (resource.items ?? []).map((item) => ({
      key: `${resource.to}-${item.id}`,
      name: item.name,
      updated_at: item.updated_at,
      to: resource.detailTo ? resource.detailTo(item) : resource.to,
      typeLabel: t_i18n(resource.label),
      icon: resource.icon,
    })))
    .sort((a, b) => stamp(b.updated_at) - stamp(a.updated_at))
    .slice(0, 6);

  if (entries.length === 0) return null;

  return (
    <div className="space-y-2">
      <h2 className="content-compact font-semibold uppercase tracking-wider text-default-secondary">
        {t_i18n("Recently modified")}
      </h2>
      <Card className="border-elevation-subtle">
        <CardContent className="p-0">
          {entries.map((entry, index) => {
            return (
              <Link
                key={entry.key}
                to={entry.to}
                className={cn(
                  "flex items-center gap-3 px-4 h-10 content-base transition-colors hover:bg-elevation-hover",
                  index > 0 && "border-t border-elevation-subtle-soft",
                )}
              >
                <Icon name={entry.icon} size={16} className="text-default-secondary" />
                <span className="truncate">{entry.name}</span>
                <span className="ml-auto shrink-0 content-compact text-default-secondary">{entry.typeLabel}</span>
                <span className="w-16 shrink-0 text-right content-compact tabular-nums text-default-secondary">
                  {timeAgo(entry.updated_at)}
                </span>
              </Link>
            );
          })}
        </CardContent>
      </Card>
    </div>
  );
}

export function ResourceHub({
  title, description, feature, resources,
}: {
  /** English page title — matches the sidebar group label. */
  title: string;
  description: string;
  feature?: HubFeature;
  resources: HubResource[];
}) {
  const { t_i18n } = useFormatter();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">{t_i18n(title)}</h1>
        <p className="mt-2 text-default-secondary">{t_i18n(description)}</p>
      </div>

      {/* Wrapped in a real element, not left as the bare card: when the Agentic
          AI gate wraps it, its `display: contents` span drops the `space-y`
          margin entirely. `pb-2` then separates the accent card from the grid by
          more than the grid's own 16px gutter, so it reads as its own block
          rather than the first tile of the grid. */}
      {feature && (
        <div className="pb-2">
          <FeatureCard feature={feature} />
        </div>
      )}

      {/* auto-rows-fr: every card of the grid takes the tallest card's height,
          so a row set never mixes heights (#10450 UX r1 A1, standards 4). */}
      <div className="grid auto-rows-fr gap-4 sm:grid-cols-2">
        {resources.map((resource) => (
          <ResourceCard key={resource.to} resource={resource} />
        ))}
      </div>

      <RecentlyModified resources={resources} />
    </div>
  );
}
