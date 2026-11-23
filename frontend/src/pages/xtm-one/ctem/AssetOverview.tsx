// SPDX-License-Identifier: Apache-2.0

/**
 * The parts of one asset's page in CTEM > Inventory (#1367, Sam 23 Nov 2026, points 6 and 8b): every section in a
 * raised card (the design system's Paper through Card), a section with nothing to list says so on ONE line under its
 * title (no empty table with its toolbar), and the overview card shows every field the inventory stores for the
 * asset with its mark, an empty field hidden, never a "-".
 *
 * What the store holds per source today (the PR body lists it): Microsoft Intune sends the serial, the Entra device
 * id, the hostname and FQDN, the Wi-Fi and Ethernet MAC addresses, the OS family and version, the last sync and the
 * user (owner); Microsoft Defender adds the IP addresses and the OS build; OpenAEV sends the category, criticality,
 * internet facing, last seen and status. Architecture, manufacturer, model and the agents or executors are not
 * captured yet, so they are not drawn.
 */
import type { ComponentProps, ReactNode } from "react";
import { Icon } from "@filigran/design-system";
import { Card } from "@/components/ui/card";
import { Tip } from "@/components/ui/tooltip";
import type { AssetFieldOrigin, AssetSourceRecord, CanonicalAssetDetail } from "@/hooks/api/useOrganizationAssetsApi";
import { useFormatter } from "@/i18n";
import { formatCount } from "@/lib/number-format";
import { cn } from "@/lib/utils";
import { assetCategoryLabel, assetFieldLabel, sourceLabel } from "./inventory";
import { CategoryMark, OsMark, SourceLogo } from "./inventory-icons";

type IconName = ComponentProps<typeof Icon>["name"];

/** One section of the asset page: a raised card, its title and actions on one row, its body or one empty line. */
export function AssetSection({
  id,
  title,
  count,
  actions,
  empty,
  emptyLabel,
  children,
}: {
  id: string;
  title: string;
  /** A figure after the title (rows held), drawn only when there are rows. */
  count?: number;
  actions?: ReactNode;
  /** The section has nothing to list: its empty line replaces the body. */
  empty: boolean;
  emptyLabel: string;
  children?: ReactNode;
}) {
  const { intl } = useFormatter();
  return (
    <Card aria-labelledby={id} role="region" data-testid={`ctem-asset-section-${id}`} className="p-4">
      <div className="flex min-h-8 items-center justify-between gap-3">
        <h2 id={id} className="title-xs flex items-center gap-2">
          {title}
          {!empty && count != null && <span className="content-caption text-default-secondary">{formatCount(intl.locale, count)}</span>}
        </h2>
        {actions}
      </div>
      {empty ? (
        <p className="pt-1 content-compact text-default-secondary" data-testid={`ctem-asset-section-${id}-empty`}>
          {emptyLabel}
        </p>
      ) : (
        <div className="pt-3">{children}</div>
      )}
    </Card>
  );
}

/** One fact of an overview card (asset or software page): its mark, its label above, its value (several values on their own lines). */
export function Fact({ icon, mark, label, values, hint }: { icon?: IconName; mark?: ReactNode; label: string; values: ReactNode[]; hint?: string }) {
  const body = (
    <div className="flex min-w-0 items-start gap-2" data-testid="ctem-asset-fact">
      <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center text-default-secondary">
        {mark ?? (icon ? <Icon name={icon} size={16} aria-hidden /> : null)}
      </span>
      <div className="min-w-0">
        <div className="content-caption text-default-secondary">{label}</div>
        {values.map((value, i) => (
          <div key={i} className="truncate content-compact font-medium">
            {value}
          </div>
        ))}
      </div>
    </div>
  );
  return hint ? <Tip content={hint}>{body}</Tip> : body;
}

/** The distinct values of one identifier kind across the asset's records, in record order. */
export function identifierValues(records: readonly AssetSourceRecord[], kind: string): string[] {
  const out: string[] = [];
  for (const record of records) {
    const raw = record.identifiers?.[kind];
    for (const value of Array.isArray(raw) ? raw : raw != null ? [raw] : []) {
      const text = String(value ?? "").trim();
      if (text && !out.includes(text)) out.push(text);
    }
  }
  return out;
}

/** A field's value as text; empty or unknown says nothing (hidden). */
export function fieldText(fields: Record<string, AssetFieldOrigin> | undefined, name: string): string {
  const value = fields?.[name]?.value;
  if (value == null) return "";
  const text = Array.isArray(value) ? value.filter(Boolean).join(", ") : String(value).trim();
  return text.toLowerCase() === "unknown" ? "" : text;
}

/** The words of the identifier kinds the overview shows. @i18n-keys */
const IDENTIFIER_LABELS = {
  hostname: "Hostname",
  fqdn: "FQDN",
  ip: "IP addresses",
  mac: "MAC addresses",
  serial: "Serial number",
  device_id: "Device id",
} as const;

/** The identifier kinds the overview shows, in reading order, each with its mark. */
const IDENTIFIER_FACTS: { kind: keyof typeof IDENTIFIER_LABELS; icon: IconName }[] = [
  { kind: "hostname", icon: "server" },
  { kind: "fqdn", icon: "earth" },
  { kind: "ip", icon: "network" },
  { kind: "mac", icon: "router" },
  { kind: "serial", icon: "tag" },
  { kind: "device_id", icon: "key-round" },
];

/** The overview card: what the asset is, then how it is identified, then who sees it and when. */
export function AssetOverviewCard({ asset }: { asset: CanonicalAssetDetail }) {
  const { t_i18n, intl } = useFormatter();
  const fields = asset.fields;
  const origin = (name: string) => {
    const source = fields?.[name]?.source;
    return source ? t_i18n("From {source}", { source: sourceLabel(t_i18n, String(source)) }) : undefined;
  };
  const date = (value: string) => intl.formatDate(value, { dateStyle: "medium", timeStyle: "short" });
  const category = assetCategoryLabel(t_i18n, asset);
  const os = [fieldText(fields, "os_family"), fieldText(fields, "os_version")].filter(Boolean).join(" ");
  const build = fieldText(fields, "os_build");
  const facts: ReactNode[] = [];
  if (category)
    facts.push(
      <Fact key="category" mark={<CategoryMark category={asset.category} subcategory={asset.subcategory} />} label={t_i18n("Category")} values={[category]} hint={origin("category")} />,
    );
  if (os)
    facts.push(
      <Fact key="os" mark={<OsMark family={fieldText(fields, "os_family")} />} label={t_i18n("Operating system")} values={[build ? `${os} (${build})` : os]} hint={origin("os_family")} />,
    );
  for (const [name, icon, label] of [
    ["criticality", "triangle-alert", "Criticality"],
    ["internet_facing", "earth", "Internet facing"],
    ["environment", "layers", "Environment"],
  ] as const) {
    const text = assetFieldLabel(t_i18n, name, fieldText(fields, name));
    if (text) facts.push(<Fact key={name} icon={icon} label={t_i18n(label)} values={[text]} hint={origin(name)} />);
  }
  const owner = fieldText(fields, "owner");
  if (owner) facts.push(<Fact key="owner" icon="user" label={t_i18n("Owner")} values={[owner]} hint={origin("owner")} />);
  const group = fieldText(fields, "group");
  if (group) facts.push(<Fact key="group" icon="custom/asset-groups" label={t_i18n("Asset group")} values={[group]} hint={origin("group")} />);
  for (const { kind, icon } of IDENTIFIER_FACTS) {
    const values = identifierValues(asset.records, kind);
    if (values.length) facts.push(<Fact key={kind} icon={icon} label={t_i18n(IDENTIFIER_LABELS[kind])} values={values} />);
  }
  const seen = asset.records.filter((r) => r.last_seen);
  if (seen.length)
    facts.push(
      <Fact
        key="last_seen"
        icon="clock"
        label={t_i18n("Last seen")}
        values={seen.map((r) => (
          <span key={`${r.source}|${r.external_id}`} className="inline-flex items-center gap-1.5">
            <SourceLogo source={r.source} />
            {sourceLabel(t_i18n, r.source)}: {date(r.last_seen!)}
          </span>
        ))}
      />,
    );
  return (
    <Card className="p-4" data-testid="ctem-asset-overview">
      {facts.length ? (
        <div className={cn("grid gap-x-6 gap-y-4", "grid-cols-1 md:grid-cols-2 xl:grid-cols-4")}>{facts}</div>
      ) : (
        <p className="content-compact text-default-secondary">{t_i18n("No source says more about this asset yet.")}</p>
      )}
    </Card>
  );
}
