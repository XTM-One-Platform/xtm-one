// SPDX-License-Identifier: Apache-2.0

/**
 * The filter fields of one organization sync (#1982 S4), shared by the add stepper (OrganizationImportDialog) and the
 * Edit dialog of a saved sync (OrganizationSyncEditDialog), so a filter reads and saves the same way in both.
 * Inbound: the filters the source supports, their values read live from it (GET /organization-context/import/options).
 * Outbound: the XTM One row fields an outbound run filters on, with the profile's own values (no platform read).
 */
import { Spinner } from "@filigran/design-system";

import { DateFilterInput } from "@/components/ui/date-filter-input";
import { Input } from "@/components/ui/input";
import { MultiSelect } from "@/components/ui/multi-select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, SettingsSwitchField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import {
  useOrganizationImportOptions,
  useOrganizationImportSources,
  useOrganizationPirs,
  type ImportFilterOption,
  type ImportSectionKey,
  type ImportSourceKey,
  type SyncDirection,
} from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { FILTER_LABELS, MIN_CRITICALITY_LABELS, filterValueLabel, keepsSourceOrder, linkOptionOn, type FilterKey } from "@/lib/organization-import-filters";
import { outboundFilterKeys, outboundFilterValues } from "@/lib/organization-outbound-filters";
import { IMPORT_SOURCE_NAMES } from "@/lib/organization-profile";

/** A sync's filters as the API takes them: filter key -> one value or a list. */
export type SyncFilters = Record<string, string | string[]>;

/** @i18n-keys */
const FILTER_HELP = {
  labels: { helper: "Only Systems with one of these labels. Empty: every label.", info: "The labels of the System in OpenCTI." },
  markings: { helper: "Only Systems with one of these markings. Empty: every marking.", info: "The data markings of the System, such as TLP:AMBER. Kept as tags on each row." },
  created_by: { helper: "Only Systems from these authors. Empty: every author.", info: "The author of the System in OpenCTI: a connector or an organization. Picks the Systems one connector brings." },
  creator: { helper: "Only Systems created by these users. Empty: every user.", info: "The OpenCTI user or connector account that created the System." },
  asset_groups: { helper: "Only assets of these groups. Empty: every group.", info: "The asset groups of OpenAEV." },
  tags: { helper: "Only entities with one of these tags. Empty: every tag.", info: "The tags of the source." },
  category: { helper: "Only assets of these categories. Empty: every category.", info: "The asset category the source gives each asset, such as Host or Cloud resource." },
  subcategory: { helper: "Only assets of these subcategories. Empty: every subcategory.", info: "Narrowed to the categories picked above, such as Server under Host." },
  criticality: { helper: "Only assets rated at one of these levels. Empty: every level.", info: "The criticality the source gives each asset." },
  internet_facing: { helper: "Only assets with this exposure. Empty: any exposure.", info: "Whether the source marks the asset reachable from the internet." },
  environment: { helper: "Only assets of these environments. Empty: every environment.", info: "The environment the source gives each asset, such as Production." },
  platform: { helper: "Only assets on these systems. Empty: every system.", info: "The operating system or platform of the asset, such as Linux or Windows." },
  type: { helper: "Only security platforms of these types. Empty: every type.", info: "OpenAEV's security platform type, such as EDR or SIEM." },
  created_after: { helper: "Only Systems created on or after this day. Empty: any date.", info: "OpenCTI's creation date of the System." },
  min_criticality: { helper: "This level and every level above it.", info: "OpenCRQ's criticality of the supporting asset. Default: High, so High and Very high are imported." },
  tenant_id: { helper: "Empty: your default OpenCRQ tenant.", info: "The OpenCRQ tenant to read, when you have several." },
  link_supporting_assets: {
    helper: "On by default. Off: primary assets are imported without their supporting assets.",
    info: "Links each primary asset to the supporting assets OpenCRQ attaches to it, matched by OpenCRQ id or name. A link you add or remove by hand always wins.",
  },
  pir_ids: { helper: "Only these PIRs. Empty: every PIR of OpenCTI.", info: "Picked from the PIRs of your connected OpenCTI." },
  search: { helper: "Only names holding this text. Empty: every name.", info: "Matched by the source on the name, as its own search does." },
  operating_system: { helper: "Only devices on these systems. Empty: every system.", info: "The operating system the source reports for the device." },
  compliance: { helper: "Only devices in these states. Empty: every state.", info: "The compliance state Intune gives the device against your policies." },
  ownership: { helper: "Only devices of this ownership. Empty: both.", info: "Whether Intune marks the device as owned by the company or by the person." },
  last_seen_days: { helper: "Only devices seen in this window. Empty: any time.", info: "The last time the source heard from the device: its last check-in or the last report of its sensor." },
  include_owner: { helper: "Off: the device's user is not copied.", info: "On: the primary user Intune names becomes the asset owner. Personal data: keep it off unless your policy allows it." },
  os_platform: { helper: "Only machines on these systems. Empty: every system.", info: "The OS platform Defender for Endpoint reports for the machine." },
  health_status: { helper: "Only machines in these states. Empty: every state.", info: "The health of the Defender sensor on the machine." },
  exposure_level: { helper: "Only machines at these levels. Empty: every level.", info: "The exposure level Defender Vulnerability Management gives the machine." },
  organizations: { helper: "Only repositories of these organizations. Empty: every repository the account reaches.", info: "The GitHub organizations the connected account is a member of." },
  groups: { helper: "Only projects of these groups, subgroups included. Empty: every project the token is a member of.", info: "The GitLab groups the connected token can read." },
  include_archived: { helper: "Off: archived repositories are left out.", info: "On: archived repositories are imported too, marked inactive." },
  include_forks: { helper: "Off: forks are left out.", info: "On: forked repositories are imported too, tagged fork." },
  skip_software: { helper: "Off: each endpoint's installed applications are read.", info: "Tanium's installed applications with their versions. On: endpoints only, faster." },
  filter_query: { helper: "Only hosts this FQL expression matches. Empty: every host.", info: "A Falcon Query Language filter on the host fields, such as tags:'SensorGroupingTags/Finance'." },
  ci_class: { helper: "The CMDB class to read. Default: Computers.", info: "The ServiceNow hardware class whose configuration items become assets." },
  operational_status: { helper: "Only CIs in these states. Empty: every state. CIs whose install status is Retired are always left out.", info: "The operational status ServiceNow gives the configuration item." },
  query: { helper: "Only CIs this encoded query matches. Empty: every CI of the class.", info: "A ServiceNow encoded query, as the list view's Copy query gives it, such as location.name=Paris." },
  skip_dependencies: { helper: "Off: each repository's dependencies are read.", info: "GitHub: the dependency graph SBOM. GitLab: the dependency list, on GitLab Ultimate only. On: repositories only, faster." },
} as const;

/**
 * The words of the filters an outbound sync offers (#1982 S4, UX round 1 on #2020 B2): on a push XTM One is the
 * source, so each filter reads the XTM One row's own value.
 * @i18n-keys
 */
const OUTBOUND_FILTER_HELP: Partial<Record<FilterKey, { helper: string; info: string }>> = {
  category: { helper: "Only XTM One assets of these categories. Empty: every category.", info: "The category of the asset row in XTM One, such as Host or Cloud resource." },
  criticality: { helper: "Only XTM One rows rated at one of these levels. Empty: every level.", info: "The criticality of the row in XTM One." },
  internet_facing: { helper: "Only XTM One rows with this exposure. Empty: any exposure.", info: "Whether the row in XTM One is marked reachable from the internet." },
  environment: { helper: "Only XTM One rows of these environments. Empty: every environment.", info: "The environment of the row in XTM One, such as Production." },
  search: { helper: "Only names holding this text. Empty: every name.", info: "Matched on the name of the XTM One row, ignoring case." },
};

/** The filters read from the source as a list of values (a picker, never free text). */
const PICKED: FilterKey[] = [
  "labels",
  "markings",
  "created_by",
  "creator",
  "asset_groups",
  "tags",
  "category",
  "subcategory",
  "criticality",
  "internet_facing",
  "environment",
  "platform",
  "type",
  "operating_system",
  "compliance",
  "ownership",
  "os_platform",
  "health_status",
  "exposure_level",
  "operational_status",
  "organizations",
  "groups",
];

/** The OpenCTI filters that need the System taxonomy (OpenCTI-Platform/opencti#19943). */
const TAXONOMY_FILTERS: FilterKey[] = ["category", "subcategory", "criticality", "internet_facing", "environment", "platform"];

interface Props {
  section: ImportSectionKey | null;
  source: ImportSourceKey | null;
  direction: SyncDirection;
  filters: SyncFilters;
  onChange: (next: SyncFilters) => void;
  /** Whether the fields are shown: the platform's values are read only then. */
  active: boolean;
  /** The caller draws a field of its own after the filters (the batch type of an OpenCTI assets import): no "no filter" line then. */
  hasOtherFields?: boolean;
}

/** The filters of one section and platform: inbound the source's own (values read live), outbound the XTM One row fields. */
export function SyncFilterFields({ section, source, direction, filters, onChange, active, hasOtherFields = false }: Props) {
  const { t_i18n } = useFormatter();
  const outbound = direction === "outbound";
  const sources = useOrganizationImportSources(active && !outbound);
  // Outbound filters read the profile's own values: the platform is not read for them.
  const options = useOrganizationImportOptions(section, source, active && !outbound);
  const pirs = useOrganizationPirs(active && section === "pirs");
  const platform = IMPORT_SOURCE_NAMES[source ?? ""] ?? source ?? "";
  const sourceEntry = sources.data?.sections.find((entry) => entry.section === section)?.sources.find((entry) => entry.source === source);
  // Outbound values are the profile's own: a cached inbound read of the same platform never disables them.
  const unavailable = outbound ? {} : (options.data?.unavailable ?? {});
  const noTaxonomy = !outbound && source === "opencti" && TAXONOMY_FILTERS.some((key) => unavailable[key] === "no_system_taxonomy");
  const shownFilters = outbound
    ? outboundFilterKeys(section)
    : (sourceEntry?.filters ?? []).filter((key): key is FilterKey => key in FILTER_LABELS && !(noTaxonomy && (TAXONOMY_FILTERS as string[]).includes(key)));

  const setFilter = (key: string, value: string | string[]) => onChange({ ...filters, [key]: value });

  const listValue = (key: string) => {
    const value = filters[key];
    return Array.isArray(value) ? value : value ? [value] : [];
  };
  const toggle = (key: string, value: string) => {
    const current = listValue(key);
    const nextValues = current.includes(value) ? current.filter((v) => v !== value) : [...current, value];
    if (key === "category") {
      // A subcategory of a category no longer picked is dropped with it.
      const parents = new Set(nextValues);
      const subs = (options.data?.options.subcategory ?? []).filter((o) => listValue("subcategory").includes(o.value) && (!o.parent || parents.size === 0 || parents.has(o.parent)));
      return onChange({ ...filters, subcategory: subs.map((o) => o.value), [key]: nextValues });
    }
    setFilter(key, nextValues);
  };

  /** The words of one value: the profile's own label for a taxonomy, an exposure or an environment, else the source's. */
  const optionLabel = (key: FilterKey, option: ImportFilterOption): string => {
    const words = filterValueLabel(t_i18n, key, option.value);
    return words === option.value ? option.label : words;
  };

  const optionsOf = (key: FilterKey) => {
    if (outbound) return outboundFilterValues(key).map((value) => ({ value, label: filterValueLabel(t_i18n, key, value) }));
    const all = options.data?.options[key] ?? [];
    const picked = listValue("category");
    const shown = key === "subcategory" && picked.length > 0 ? all.filter((o) => !o.parent || picked.includes(o.parent)) : all;
    return shown.map((o) => ({ value: o.value, label: optionLabel(key, o) }));
  };

  const reasonOf = (key: FilterKey): string | null => {
    const code = unavailable[key];
    if (!code) return null;
    if (code === "not_connected") return t_i18n("{platform} is not connected for your account.", { platform });
    return t_i18n("{platform} did not list these values. Try again in a moment.", { platform });
  };

  const help = (key: FilterKey) => (outbound ? (OUTBOUND_FILTER_HELP[key] ?? FILTER_HELP[key]) : FILTER_HELP[key]);

  const filterControl = (key: FilterKey) => {
    const id = `org-import-filter-${key}`;
    const reason = reasonOf(key);
    // An on/off choice is a switch row of its own, never a control inside a labelled field.
    if (key === "include_owner" || key === "include_archived" || key === "include_forks" || key === "skip_dependencies" || key === "skip_software")
      return (
        <SettingsSwitchField
          key={key}
          id={id}
          label={t_i18n(FILTER_LABELS[key])}
          helper={t_i18n(FILTER_HELP[key].helper)}
          info={t_i18n(FILTER_HELP[key].info)}
          checked={filters[key] === "true"}
          onCheckedChange={(checked) => setFilter(key, checked ? "true" : "")}
        />
      );
    // On unless "false" (#1591): the one default-on switch, stored only when turned off.
    if (key === "link_supporting_assets")
      return (
        <SettingsSwitchField
          key={key}
          id={id}
          label={t_i18n(FILTER_LABELS[key])}
          helper={t_i18n(FILTER_HELP[key].helper)}
          info={t_i18n(FILTER_HELP[key].info)}
          checked={linkOptionOn(filters)}
          onCheckedChange={(checked) => setFilter(key, checked ? "" : "false")}
        />
      );
    const control = () => {
      if ((PICKED as string[]).includes(key))
        return (
          <MultiSelect
            id={id}
            aria-label={t_i18n(FILTER_LABELS[key])}
            aria-describedby={settingsFieldHintId(id)}
            options={optionsOf(key)}
            selected={listValue(key)}
            onToggle={(value) => toggle(key, value)}
            disabled={Boolean(reason)}
            sort={!outbound && !keepsSourceOrder(key)}
            placeholder={!outbound && options.isLoading ? t_i18n("Reading {platform}", { platform }) : t_i18n("Every value")}
            searchPlaceholder={t_i18n("Search a value")}
            emptyOptionsLabel={t_i18n("No value in {platform}", { platform })}
            noMatchLabel={t_i18n("No matching value")}
          />
        );
      if (key === "min_criticality" || key === "tenant_id" || key === "last_seen_days" || key === "ci_class") {
        const choices =
          key === "min_criticality"
            ? (Object.keys(MIN_CRITICALITY_LABELS) as (keyof typeof MIN_CRITICALITY_LABELS)[]).map((level) => ({ value: level, label: t_i18n(MIN_CRITICALITY_LABELS[level]) }))
            : key === "last_seen_days"
              ? [{ value: "", label: t_i18n("Any time") }, ...(options.data?.options.last_seen_days ?? []).map((o) => ({ value: o.value, label: filterValueLabel(t_i18n, key, o.value) }))]
              : key === "ci_class"
                ? (options.data?.options.ci_class ?? []).map((o) => ({ value: o.value, label: filterValueLabel(t_i18n, key, o.value) }))
                : (options.data?.options.tenant_id ?? []).map((o) => ({ value: o.value, label: o.label }));
        const current = typeof filters[key] === "string" ? (filters[key] as string) : "";
        return (
          <SearchableSelect
            id={id}
            aria-labelledby={settingsFieldLabelId(id)}
            aria-describedby={settingsFieldHintId(id)}
            sort={false}
            disabled={Boolean(reason)}
            options={choices}
            value={current || (key === "min_criticality" ? "high" : key === "ci_class" ? "cmdb_ci_computer" : "")}
            onValueChange={(value) => setFilter(key, value)}
            placeholder={key === "tenant_id" ? t_i18n("Default tenant") : undefined}
            className="w-full"
          />
        );
      }
      if (key === "pir_ids")
        return (
          <MultiSelect
            id={id}
            aria-label={t_i18n(FILTER_LABELS.pir_ids)}
            aria-describedby={settingsFieldHintId(id)}
            options={(pirs.data?.pirs ?? []).map((pir) => ({ value: pir.id, label: pir.name }))}
            selected={listValue(key)}
            onToggle={(value) => toggle(key, value)}
            placeholder={t_i18n("Every PIR")}
            searchPlaceholder={t_i18n("Search a PIR")}
            emptyOptionsLabel={t_i18n("No PIR in OpenCTI")}
            noMatchLabel={t_i18n("No matching PIR")}
          />
        );
      if (key === "created_after")
        return (
          <DateFilterInput
            id={id}
            aria-labelledby={settingsFieldLabelId(id)}
            aria-describedby={settingsFieldHintId(id)}
            value={typeof filters[key] === "string" ? (filters[key] as string) : ""}
            onChange={(value) => setFilter(key, value)}
          />
        );
      return (
        <Input
          id={id}
          aria-labelledby={settingsFieldLabelId(id)}
          aria-describedby={settingsFieldHintId(id)}
          type="text"
          value={typeof filters[key] === "string" ? (filters[key] as string) : ""}
          onChange={(e) => setFilter(key, e.target.value)}
        />
      );
    };
    return (
      <SettingsField key={key} id={id} label={t_i18n(FILTER_LABELS[key])} helper={reason ?? t_i18n(help(key).helper)} info={t_i18n(help(key).info)}>
        {control()}
      </SettingsField>
    );
  };

  if (!outbound && sources.isLoading)
    return (
      <p className="flex items-center gap-2 content-compact text-default-secondary">
        <Spinner size="sm" />
        {t_i18n("Checking the connected platforms")}
      </p>
    );
  return (
    <>
      {shownFilters.length === 0 && !hasOtherFields && <p className="content-compact text-default-secondary">{t_i18n("This source has no filter for this section: every entity is read.")}</p>}
      {noTaxonomy && (
        <p className="content-compact text-default-secondary" data-testid="org-import.no-taxonomy">
          {t_i18n("This OpenCTI gives Systems no category, criticality, exposure, environment or platform: those filters are hidden and every row takes the type below. Update OpenCTI to read them.")}
        </p>
      )}
      {shownFilters.map(filterControl)}
    </>
  );
}
