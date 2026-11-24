// SPDX-License-Identifier: Apache-2.0

/**
 * "From the platforms" on the organization profile (#11780, #11886): a stepper
 * that imports one section of the profile from one connected platform, with no
 * model call, so Community Edition uses it too.
 *
 * Steps: the section, the source (the product's own logo; a platform not
 * connected or not readable for this administrator is shown disabled with the
 * reason), the filters that source supports as pickers fed live from it
 * (GET /organization-context/import/options), the mapping of each profile field
 * with its source attribute, its fallback and an example read from the source,
 * then the preview: the source's own count, N new, N updated, N unchanged and
 * N skipped rows, the rows a previous import wrote that the source no longer
 * returns (kept unless the administrator removes them), and a sample. "Import"
 * saves the next profile version; rows typed by hand are never overwritten.
 * "Keep in sync" on the preview also saves the import as a scheduled sync
 * (#11913), listed under Settings > Organization > Syncs.
 */
import { useState, type ElementType, type KeyboardEvent } from "react";
import { Icon, Spinner, type IconName } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { CompactFigure } from "@/components/ui/compact-figure";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { MultiSelect } from "@/components/ui/multi-select";
import { OptionCard } from "@/components/ui/option-card";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, SettingsSwitchField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { useToast } from "@/components/ui/toast";
import { useMe } from "@/hooks/api/useAuthApi";
import {
  useApplyOrganizationImport,
  useCreateOrganizationSync,
  useOrganizationImportOptions,
  useOrganizationImportSources,
  useOrganizationPirs,
  usePreviewOrganizationImport,
  type ImportFilterOption,
  type ImportPreviewResponse,
  type ImportSectionKey,
  type ImportSourceKey,
} from "@/hooks/api/useOrganizationContextApi";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { ASSET_CRITICALITY_LABELS, typeLabel, typeOptions } from "@/lib/asset-taxonomy";
import {
  ENVIRONMENT_LABELS,
  EXPOSURE_LABELS,
  FILTER_LABELS,
  MIN_CRITICALITY_LABELS,
  filterValueLabel,
  keepsSourceOrder,
  type FilterKey,
} from "@/lib/organization-import-filters";
import { IMPORT_SOURCE_NAMES, IMPORT_SOURCE_PROVIDER } from "@/lib/organization-profile";
import { defaultSchedule, type SyncSchedule } from "@/lib/organization-sync-schedule";
import { OrganizationSharedContextNotice } from "./OrganizationSharedContextNotice";
import { SyncScheduleFields } from "./OrganizationSyncSchedule";

/** A library glyph handed to OptionCard, which takes a glyph component (no direct lucide-react import). */
const glyph = (name: IconName): ElementType => {
  const Glyph = ({ className }: { className?: string }) => <Icon name={name} size={16} className={className} />;
  return Glyph;
};

/** The product's own logo on a source card (the shared ProviderLogo assets); the card's title names it. */
const logo = (source: ImportSourceKey): ElementType => {
  const Logo = () => <ProviderLogo provider={IMPORT_SOURCE_PROVIDER[source] ?? source} decorative className="h-4 w-4" />;
  return Logo;
};
const SOURCE_LOGOS: Record<ImportSourceKey, ElementType> = {
  opencti: logo("opencti"),
  openaev: logo("openaev"),
  opencrq: logo("opencrq"),
  intune: logo("intune"),
  defender: logo("defender"),
  tanium: logo("tanium"),
  crowdstrike: logo("crowdstrike"),
  servicenow: logo("servicenow"),
  github: logo("github"),
  gitlab: logo("gitlab"),
};

/** @i18n-keys */
const SECTION_LABELS = {
  assets: "Assets",
  asset_groups: "Asset groups",
  ai_systems: "AI systems",
  tech_stack: "Tech stack",
  crown_jewels: "Crown jewels",
  pirs: "PIRs",
} as const satisfies Record<ImportSectionKey, string>;

/** One line each, of the same length, so the six cards read alike. @i18n-keys */
const SECTION_HINTS = {
  assets: "Hosts, applications and services to protect.",
  asset_groups: "The groups your assets are organized into.",
  ai_systems: "The AI models and agents your teams test.",
  tech_stack: "The security products already in place.",
  crown_jewels: "The assets your business cannot afford to lose.",
  pirs: "The intelligence needs your agents follow.",
} as const satisfies Record<ImportSectionKey, string>;

const SECTION_GLYPHS: Record<ImportSectionKey, ElementType> = {
  assets: glyph("server"),
  asset_groups: glyph("network"),
  ai_systems: glyph("bot"),
  tech_stack: glyph("shield-check"),
  crown_jewels: glyph("key-round"),
  pirs: glyph("file-text"),
};

/** Arrow keys move the choice to the next enabled card, as in a native radio group (the selected card is the one tab stop). */
function onRadioKeyDown<T extends string>(event: KeyboardEvent<HTMLDivElement>, keys: T[], current: T | null, choose: (key: T) => void, idOf: (key: T) => string) {
  const step = event.key === "ArrowDown" || event.key === "ArrowRight" ? 1 : event.key === "ArrowUp" || event.key === "ArrowLeft" ? -1 : 0;
  if (step === 0 || keys.length === 0) return;
  event.preventDefault();
  const from = current === null ? -1 : keys.indexOf(current);
  const next = keys[(from + step + keys.length) % keys.length];
  choose(next);
  document.getElementById(idOf(next))?.focus();
}

/** @i18n-keys */
const STEP_TITLES = {
  section: "What to import",
  source: "Source",
  filters: "Filters",
  mapping: "Mapping",
  preview: "Preview",
} as const;
type Step = keyof typeof STEP_TITLES;
const STEPS: Step[] = ["section", "source", "filters", "mapping", "preview"];

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

/** @i18n-keys */
const OUTCOME_LABELS = { new: "New", updated: "Updated", unchanged: "Unchanged", skipped: "Skipped" } as const;

/** @i18n-keys */
const PROFILE_FIELDS = {
  name: "Name",
  type: "Type",
  criticality: "Criticality",
  exposure: "Exposure",
  environment: "Environment",
  owner: "Owner",
  group: "Group",
  tags: "Tags",
  external_id: "External ID",
  product: "Product",
  kind: "Kind",
  hostname: "Hostname",
  fqdn: "FQDN",
  serial: "Serial number",
  device_id: "Entra device ID",
  os_family: "OS family",
  os_version: "OS version",
  os_build: "OS build",
  last_seen: "Last seen",
  resource_id: "Resource ID",
  url: "URL",
  software: "Software",
} as const;
type ProfileField = keyof typeof PROFILE_FIELDS;

/** @i18n-keys */
const MAPPING_NOTES = {
  batchType: "The type you pick on the previous step",
  createdBy: "createdBy, when the System names no owner",
  platformMarkings: "With the platform and the markings, as platform: and marking: tags",
  aiKind: "Always Model API",
  aiExposure: "Public when internet-facing, else internal",
  veryHighCritical: "Very high becomes Critical",
  deviceOwner: "Only when Device owner is on",
  osTaxonomy: "From the operating system, and the device model when the source gives it",
  deviceTags: "OS, compliance and ownership, each as a prefixed tag",
  machineTags: "OS, exposure level, health and risk, each as a prefixed tag",
  hostDomain: "The host name with the machine domain, when the source gives one",
  ciDisplay: "The display value ServiceNow shows for the reference",
  ciClassTag: "The CI class as a ci_class: tag",
  codeRepository: "Always Source code, Code repository",
  repoExposure: "Internet-facing when public, else not internet-facing",
  repoTags: "Owner, visibility, language, topics and default branch as prefixed tags, plus archived and fork when they apply",
  repoTagsGitlab: "Owner, visibility, topics and default branch as prefixed tags, plus archived and fork when they apply",
  repoKey: "The host and the repository's numeric ID, kept across a rename",
  githubSbom: "The dependency graph SBOM (SPDX): name, version, PURL",
  gitlabDependencies: "The dependency list (GitLab Ultimate): name, version, PURL from the package manager",
} as const;

/** One profile field: the source attribute(s) it reads, and the fallback when the source gives none. */
interface MappingRow {
  to: ProfileField;
  from: string[];
  fallback?: keyof typeof MAPPING_NOTES;
  note?: keyof typeof MAPPING_NOTES;
}

const ASSET_FIELDS: ProfileField[] = ["name", "type", "criticality", "exposure", "environment", "owner", "group", "tags", "external_id"];
/** An endpoint source also fills the identity keys and the operating system of the asset (#1251). */
const DEVICE_FIELDS: ProfileField[] = [...ASSET_FIELDS, "hostname", "fqdn", "serial", "device_id", "os_family", "os_version", "os_build", "last_seen"];
/** A code source fills the repository's identity key, its link and its dependencies. */
const REPOSITORY_FIELDS: ProfileField[] = [...ASSET_FIELDS, "resource_id", "url", "last_seen", "software"];

/** How the source's attributes map to the profile, per section and source; the profile fields absent here are "not provided". */
const MAPPINGS: Record<string, { fields: ProfileField[]; rows: MappingRow[] }> = {
  "assets:openaev": {
    fields: ASSET_FIELDS,
    rows: [
      { to: "name", from: ["asset_name"] },
      { to: "type", from: ["asset_category", "asset_subcategory"] },
      { to: "criticality", from: ["asset_criticality"] },
      { to: "exposure", from: ["asset_internet_facing"] },
      { to: "external_id", from: ["asset_id"] },
    ],
  },
  "assets:opencti": {
    fields: ASSET_FIELDS,
    rows: [
      { to: "name", from: ["name"] },
      { to: "type", from: ["x_opencti_system_category", "x_opencti_system_subcategory"], fallback: "batchType" },
      { to: "criticality", from: ["x_opencti_criticality"] },
      { to: "exposure", from: ["x_opencti_internet_facing"] },
      { to: "environment", from: ["x_opencti_environment"] },
      { to: "owner", from: ["x_opencti_owner"], fallback: "createdBy" },
      { to: "tags", from: ["objectLabel"], note: "platformMarkings" },
      { to: "external_id", from: ["id"] },
    ],
  },
  "assets:intune": {
    fields: DEVICE_FIELDS,
    rows: [
      { to: "name", from: ["deviceName"] },
      { to: "type", from: ["operatingSystem", "chassisType", "model"], note: "osTaxonomy" },
      { to: "owner", from: ["userDisplayName"], note: "deviceOwner" },
      { to: "tags", from: ["operatingSystem", "complianceState", "managedDeviceOwnerType"], note: "deviceTags" },
      { to: "external_id", from: ["id"] },
      { to: "hostname", from: ["deviceName"] },
      { to: "fqdn", from: ["deviceName"] },
      { to: "serial", from: ["serialNumber"] },
      { to: "device_id", from: ["azureADDeviceId"] },
      { to: "os_family", from: ["operatingSystem"] },
      { to: "os_version", from: ["osVersion"] },
      { to: "last_seen", from: ["lastSyncDateTime"] },
    ],
  },
  "assets:defender": {
    fields: DEVICE_FIELDS,
    rows: [
      { to: "name", from: ["computerDnsName"] },
      { to: "type", from: ["osPlatform"], note: "osTaxonomy" },
      { to: "tags", from: ["osPlatform", "exposureLevel", "healthStatus", "riskScore"], note: "machineTags" },
      { to: "external_id", from: ["id"] },
      { to: "hostname", from: ["computerDnsName"] },
      { to: "fqdn", from: ["computerDnsName"] },
      { to: "device_id", from: ["aadDeviceId"] },
      { to: "os_family", from: ["osPlatform"] },
      { to: "os_version", from: ["version"] },
      { to: "os_build", from: ["osBuild"] },
      { to: "last_seen", from: ["lastSeen"] },
    ],
  },
  "assets:tanium": {
    fields: DEVICE_FIELDS,
    rows: [
      { to: "name", from: ["name"] },
      { to: "type", from: ["os.name", "os.platform"], note: "osTaxonomy" },
      { to: "tags", from: ["os.platform"] },
      { to: "external_id", from: ["id"] },
      { to: "hostname", from: ["name"] },
      { to: "fqdn", from: ["name", "domainName"], note: "hostDomain" },
      { to: "serial", from: ["serialNumber"] },
      { to: "os_family", from: ["os.platform"] },
      { to: "os_version", from: ["os.generation"] },
      { to: "last_seen", from: ["eidLastSeen"] },
    ],
  },
  "assets:crowdstrike": {
    fields: DEVICE_FIELDS,
    rows: [
      { to: "name", from: ["hostname"] },
      { to: "type", from: ["os_product_name", "product_type_desc"], note: "osTaxonomy" },
      { to: "tags", from: ["platform_name", "tags"] },
      { to: "external_id", from: ["device_id"] },
      { to: "hostname", from: ["hostname"] },
      { to: "fqdn", from: ["hostname", "machine_domain"], note: "hostDomain" },
      { to: "serial", from: ["serial_number"] },
      { to: "os_family", from: ["platform_name"] },
      { to: "os_version", from: ["os_version"] },
      { to: "os_build", from: ["os_build"] },
      { to: "last_seen", from: ["last_seen"] },
    ],
  },
  "assets:servicenow": {
    fields: DEVICE_FIELDS,
    rows: [
      { to: "name", from: ["name"] },
      { to: "type", from: ["os", "sys_class_name"], note: "osTaxonomy" },
      { to: "environment", from: ["environment"] },
      { to: "owner", from: ["owned_by", "managed_by"], note: "ciDisplay" },
      { to: "tags", from: ["sys_class_name"], note: "ciClassTag" },
      { to: "external_id", from: ["sys_id"] },
      { to: "hostname", from: ["host_name"] },
      { to: "fqdn", from: ["fqdn"] },
      { to: "serial", from: ["serial_number"] },
      { to: "os_family", from: ["os"] },
      { to: "os_version", from: ["os_version"] },
      { to: "last_seen", from: ["last_discovered"] },
    ],
  },
  "assets:github": {
    fields: REPOSITORY_FIELDS,
    rows: [
      { to: "name", from: ["full_name"] },
      { to: "type", from: [], note: "codeRepository" },
      { to: "exposure", from: ["visibility"], note: "repoExposure" },
      { to: "tags", from: ["owner.login", "visibility", "language", "topics", "default_branch"], note: "repoTags" },
      { to: "external_id", from: ["id"] },
      { to: "resource_id", from: ["id"], note: "repoKey" },
      { to: "url", from: ["html_url"] },
      { to: "last_seen", from: ["pushed_at"] },
      { to: "software", from: ["dependency-graph/sbom"], note: "githubSbom" },
    ],
  },
  "assets:gitlab": {
    fields: REPOSITORY_FIELDS,
    rows: [
      { to: "name", from: ["path_with_namespace"] },
      { to: "type", from: [], note: "codeRepository" },
      { to: "exposure", from: ["visibility"], note: "repoExposure" },
      { to: "tags", from: ["namespace.full_path", "visibility", "topics", "default_branch"], note: "repoTagsGitlab" },
      { to: "external_id", from: ["id"] },
      { to: "resource_id", from: ["id"], note: "repoKey" },
      { to: "url", from: ["web_url"] },
      { to: "last_seen", from: ["last_activity_at"] },
      { to: "software", from: ["dependencies"], note: "gitlabDependencies" },
    ],
  },
  "asset_groups:openaev": { fields: ["name", "external_id"], rows: [{ to: "name", from: ["asset_group_name"] }, { to: "external_id", from: ["asset_group_id"] }] },
  // #1556: the groups of the endpoint sources, their members kept in sync at each run.
  "asset_groups:intune": {
    fields: ["name", "external_id"],
    rows: [
      { to: "name", from: ["displayName"] },
      { to: "external_id", from: ["id"] },
    ],
  },
  "asset_groups:tanium": {
    fields: ["name", "external_id"],
    rows: [
      { to: "name", from: ["name"] },
      { to: "external_id", from: ["id"] },
    ],
  },
  "asset_groups:crowdstrike": {
    fields: ["name", "external_id"],
    rows: [
      { to: "name", from: ["name"] },
      { to: "external_id", from: ["id"] },
    ],
  },
  "asset_groups:servicenow": {
    fields: ["name", "external_id"],
    rows: [
      { to: "name", from: ["cmdb_ci_service.name"] },
      { to: "external_id", from: ["sys_id"] },
    ],
  },
  "ai_systems:openaev": {
    fields: ["name", "kind", "exposure", "owner"],
    rows: [
      { to: "name", from: ["asset_name"] },
      { to: "kind", from: [], note: "aiKind" },
      { to: "exposure", from: ["asset_internet_facing"], note: "aiExposure" },
    ],
  },
  "tech_stack:openaev": { fields: ["product"], rows: [{ to: "product", from: ["security_platform_name"] }] },
  "crown_jewels:opencrq": {
    fields: ["name", "criticality", "owner"],
    rows: [
      { to: "name", from: ["name"] },
      { to: "criticality", from: ["criticality"], note: "veryHighCritical" },
    ],
  },
  "pirs:opencti": { fields: ["name"], rows: [{ to: "name", from: ["name"] }] },
};

/** The OpenCTI filters that need the System taxonomy (OpenCTI-Platform/opencti#19943). */
const TAXONOMY_FILTERS: FilterKey[] = ["category", "subcategory", "criticality", "internet_facing", "environment", "platform"];

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Today's one-click draft of the empty fields from every platform; without it the Quick setup row is not drawn. */
  onQuickSetup?: () => void;
  quickSetupPending?: boolean;
  /**
   * The sections offered (#1367, points 4 and 5): CTEM > Inventory > Sources imports the inventory sections, Settings >
   * Organization the business profile ones (crown jewels, PIRs). Every section when omitted.
   */
  sections?: readonly ImportSectionKey[];
}

/** The sections each surface imports into (#1367): the same split as the backend's SECTION_SCOPES. */
export const INVENTORY_IMPORT_SECTIONS = ["assets", "asset_groups", "tech_stack", "ai_systems"] as const satisfies readonly ImportSectionKey[];
export const PROFILE_IMPORT_SECTIONS = ["crown_jewels", "pirs"] as const satisfies readonly ImportSectionKey[];

export function OrganizationImportDialog({ open, onOpenChange, onQuickSetup, quickSetupPending, sections: offered }: Props) {
  const { t_i18n } = useFormatter();
  const f = useNumberFormat();
  const { toast } = useToast();
  const sources = useOrganizationImportSources(open);
  const preview = usePreviewOrganizationImport();
  const apply = useApplyOrganizationImport();
  const createSync = useCreateOrganizationSync();
  // #11994: the import reads, and a sync runs, with the signed-in administrator's permissions.
  const { data: me } = useMe(open);
  const meLabel = me?.email || me?.full_name || t_i18n("you");

  const [step, setStep] = useState<Step>("section");
  const [section, setSection] = useState<ImportSectionKey | null>(null);
  const [source, setSource] = useState<ImportSourceKey | null>(null);
  const [filters, setFilters] = useState<Record<string, string | string[]>>({});
  // The batch's type on OpenAEV's taxonomy (#11781): the fallback of a row the source gives no category.
  const [batchType, setBatchType] = useState("generic_asset");
  const [removeGone, setRemoveGone] = useState(false);
  const [result, setResult] = useState<ImportPreviewResponse | null>(null);
  // "Import once" (off) or "Import and keep in sync" (on, #11913) with its schedule.
  const [keepSync, setKeepSync] = useState(false);
  const [schedule, setSchedule] = useState<SyncSchedule>(defaultSchedule);
  const pirs = useOrganizationPirs(open && section === "pirs");
  const options = useOrganizationImportOptions(section, source, open && (step === "filters" || step === "mapping"));

  const platform = IMPORT_SOURCE_NAMES[source ?? ""] ?? source ?? "";
  const sectionEntry = sources.data?.sections.find((entry) => entry.section === section);
  const sourceEntry = sectionEntry?.sources.find((entry) => entry.source === source);
  const unavailable = options.data?.unavailable ?? {};
  const noTaxonomy = source === "opencti" && TAXONOMY_FILTERS.some((key) => unavailable[key] === "no_system_taxonomy");
  const shownFilters = (sourceEntry?.filters ?? []).filter(
    (key): key is FilterKey => key in FILTER_LABELS && !(noTaxonomy && (TAXONOMY_FILTERS as string[]).includes(key)),
  );
  const needsBatchType = section === "assets" && source === "opencti";
  const stepIndex = STEPS.indexOf(step);
  const body = () => ({
    section: section as ImportSectionKey,
    source: source as ImportSourceKey,
    filters,
    batch_type: needsBatchType ? batchType : "",
    ...(removeGone ? { remove_gone: true } : {}),
  });

  const close = (next: boolean) => {
    if (!next) {
      setStep("section");
      setSection(null);
      setSource(null);
      setFilters({});
      setRemoveGone(false);
      setResult(null);
      setKeepSync(false);
      setSchedule(defaultSchedule());
      preview.reset();
      apply.reset();
      createSync.reset();
    }
    onOpenChange(next);
  };

  const pickSection = (next: ImportSectionKey) => {
    setSection(next);
    // The source and its filters belong to the section: a new section starts them over.
    const first = sources.data?.sections.find((entry) => entry.section === next)?.sources.find((entry) => entry.available);
    setSource(first?.source ?? null);
    setFilters({});
    setResult(null);
  };

  // The preview is read when the mapping opens, so the mapping shows an example per field; "Preview" then shows it.
  const readPreview = () => {
    setResult(null);
    preview.mutate(body(), { onSuccess: setResult });
  };

  const showPreview = () => {
    // A failed import belongs to the previous preview: a new preview starts with no error line.
    apply.reset();
    if (!result && !preview.isPending) readPreview();
    setStep("preview");
  };

  /** The import saved as a sync: the same source, filters, batch type and removal policy, on the schedule picked. */
  const saveSync = (imported: boolean) => {
    const { section: syncSection, source: syncSource, filters: syncFilters, batch_type } = body();
    createSync.mutate(
      { section: syncSection, source: syncSource, filters: syncFilters, batch_type, remove_gone: removeGone, ...schedule, enabled: true },
      {
        onSuccess: () => {
          toast({ title: imported ? t_i18n("Profile saved and kept in sync") : t_i18n("Sync saved"), variant: "success" });
          close(false);
        },
      },
    );
  };

  const runImport = () => {
    if (!result) return;
    createSync.reset();
    // Nothing new to import today: the sync alone is saved, its runs import what comes later.
    // The import already saved and only the sync failed: saving the sync again, never a second import.
    if (keepSync && (importCount === 0 || apply.isSuccess)) return saveSync(apply.isSuccess);
    apply.mutate(
      { ...body(), based_on_version: result.based_on_version },
      {
        onSuccess: (saved) => {
          if (keepSync) return saveSync(saved.applied);
          if (saved.applied) toast({ title: t_i18n("Organization profile saved"), variant: "success" });
          close(false);
        },
      },
    );
  };

  const next = () => {
    if (step === "mapping") return showPreview();
    // The sources may have loaded after the section was picked: the first readable one is proposed then.
    if (step === "section" && source === null) setSource(sectionEntry?.sources.find((entry) => entry.available)?.source ?? null);
    if (step === "filters" && !result) readPreview();
    setStep(STEPS[stepIndex + 1]);
  };
  const canNext =
    (step === "section" && section !== null) ||
    (step === "source" && source !== null && sourceEntry?.available === true) ||
    step === "filters" ||
    step === "mapping";

  const setFilter = (key: string, value: string | string[]) => {
    setResult(null);
    setFilters((prev) => ({ ...prev, [key]: value }));
  };

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
      setFilters((prev) => ({ ...prev, subcategory: subs.map((o) => o.value) }));
    }
    setFilter(key, nextValues);
  };

  /** The words of one value: the profile's own label for a taxonomy, an exposure or an environment, else the source's. */
  const optionLabel = (key: FilterKey, option: ImportFilterOption): string => {
    const words = filterValueLabel(t_i18n, key, option.value);
    return words === option.value ? option.label : words;
  };

  const optionsOf = (key: FilterKey) => {
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
            sort={!keepsSourceOrder(key)}
            placeholder={options.isLoading ? t_i18n("Reading {platform}", { platform }) : t_i18n("Every value")}
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
      <SettingsField key={key} id={id} label={t_i18n(FILTER_LABELS[key])} helper={reason ?? t_i18n(FILTER_HELP[key].helper)} info={t_i18n(FILTER_HELP[key].info)}>
        {control()}
      </SettingsField>
    );
  };

  const sectionKeys = (Object.keys(SECTION_LABELS) as ImportSectionKey[]).filter((key) => !offered || offered.includes(key));
  const availableSources = (sectionEntry?.sources ?? []).filter((entry) => entry.available).map((entry) => entry.source);
  const pickSource = (next: ImportSourceKey) => {
    setSource(next);
    setFilters({});
    setResult(null);
  };
  const choiceId = (key: string) => `org-import-choice-${key}`;

  // The shared radio card (OptionCard); auto-rows-fr makes every row of the grid one height, so the six cards match.
  const sectionStep = (
    <div className="space-y-3">
      <div
        role="radiogroup"
        aria-label={t_i18n(STEP_TITLES.section)}
        className="grid auto-rows-fr gap-2 sm:grid-cols-2"
        data-testid="org-import.sections"
        onKeyDown={(event) => onRadioKeyDown(event, sectionKeys, section, pickSection, choiceId)}
      >
        {sectionKeys.map((key) => (
          <OptionCard
            key={key}
            id={choiceId(key)}
            selected={section === key}
            tabIndex={section === key || (section === null && key === sectionKeys[0]) ? 0 : -1}
            onSelect={() => pickSection(key)}
            icon={SECTION_GLYPHS[key]}
            title={t_i18n(SECTION_LABELS[key])}
            description={t_i18n(SECTION_HINTS[key])}
          />
        ))}
      </div>
      {onQuickSetup && (
        <div className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle px-3 py-2">
          <p className="content-compact text-default-secondary">
            {t_i18n("Quick setup: fill the empty fields from every connected platform in one click, then review them in the form.")}
          </p>
          <Button variant="secondary" onClick={onQuickSetup} disabled={quickSetupPending} aria-busy={quickSetupPending || undefined}>
            {quickSetupPending && <Spinner size="sm" tone="inherit" />}
            {t_i18n("Quick setup")}
          </Button>
        </div>
      )}
    </div>
  );

  const sourceStep = (
    <div className="space-y-2">
      <OrganizationSharedContextNotice className="mb-2" elevation={1} />
      {sources.isLoading && (
        <p className="flex items-center gap-2 content-compact text-default-secondary">
          <Spinner size="sm" />
          {t_i18n("Checking the connected platforms")}
        </p>
      )}
      {sources.isError && <p className="content-compact text-feedback-error-primary">{t_i18n("The connected platforms could not be listed. Try again in a moment.")}</p>}
      <div
        role="radiogroup"
        aria-label={t_i18n(STEP_TITLES.source)}
        className="grid gap-2"
        onKeyDown={(event) => onRadioKeyDown(event, availableSources, source, pickSource, choiceId)}
      >
        {(sectionEntry?.sources ?? []).map((entry) => (
          <OptionCard
            key={entry.source}
            id={choiceId(entry.source)}
            selected={source === entry.source}
            tabIndex={source === entry.source ? 0 : -1}
            disabled={!entry.available}
            onSelect={() => pickSource(entry.source)}
            icon={SOURCE_LOGOS[entry.source] ?? glyph("database")}
            title={IMPORT_SOURCE_NAMES[entry.source] ?? entry.source}
            description={
              entry.available
                ? t_i18n("Connected and readable with your account.")
                : entry.reason_code === "not_connected"
                  ? t_i18n("Not available: no {source} integration is connected. Connect it in Settings > Integrations.", { source: IMPORT_SOURCE_NAMES[entry.source] ?? entry.source })
                  : entry.reason_code === "no_token"
                    ? t_i18n("Not available: the {source} integration has no token. Authorize it again in Settings > Integrations.", { source: IMPORT_SOURCE_NAMES[entry.source] ?? entry.source })
                    : t_i18n("Not available: {reason}. Connect it in Settings > Integrations.", { reason: entry.reason || t_i18n("not connected") })
            }
          />
        ))}
      </div>
    </div>
  );

  const filtersStep = (
    <div className="space-y-4">
      {shownFilters.length === 0 && !needsBatchType && <p className="content-compact text-default-secondary">{t_i18n("This source has no filter for this section: every entity is read.")}</p>}
      {noTaxonomy && (
        <p className="content-compact text-default-secondary" data-testid="org-import.no-taxonomy">
          {t_i18n("This OpenCTI gives Systems no category, criticality, exposure, environment or platform: those filters are hidden and every row takes the type below. Update OpenCTI to read them.")}
        </p>
      )}
      {shownFilters.map(filterControl)}
      {needsBatchType && (
        <SettingsField
          id="org-import-batch-type"
          label={t_i18n("Type when OpenCTI gives none")}
          helper={t_i18n("Only for Systems with no category in OpenCTI; the others keep their own.")}
          info={t_i18n("Run one import per type, each with its own filters, to type a mixed inventory.")}
        >
          <SearchableSelect
            id="org-import-batch-type"
            aria-labelledby={settingsFieldLabelId("org-import-batch-type")}
            aria-describedby={settingsFieldHintId("org-import-batch-type")}
            searchable
            sort={false}
            options={typeOptions(t_i18n)}
            value={batchType}
            onValueChange={setBatchType}
            className="w-full"
          />
        </SettingsField>
      )}
    </div>
  );

  const plan = result?.plan;
  const sample = plan?.sample[0] as Record<string, unknown> | undefined;
  /** The example a field takes in the first row the source returned, in the profile's words. */
  const example = (field: ProfileField): string => {
    if (!sample) return "";
    if (field === "type") return typeLabel(t_i18n, String(sample.category ?? ""), String(sample.subcategory ?? ""));
    if (field === "criticality") {
      const value = String(sample.criticality ?? "");
      return value in ASSET_CRITICALITY_LABELS ? t_i18n(ASSET_CRITICALITY_LABELS[value as keyof typeof ASSET_CRITICALITY_LABELS]) : value;
    }
    if (field === "exposure") {
      const value = String(sample.internet_facing ?? sample.exposure ?? "");
      return value in EXPOSURE_LABELS ? t_i18n(EXPOSURE_LABELS[value as keyof typeof EXPOSURE_LABELS]) : value;
    }
    if (field === "environment") {
      const value = String(sample.environment ?? "");
      return value in ENVIRONMENT_LABELS ? t_i18n(ENVIRONMENT_LABELS[value as keyof typeof ENVIRONMENT_LABELS]) : "";
    }
    const value = sample[field];
    return Array.isArray(value) ? value.join(", ") : String(value ?? "");
  };
  const mapping = MAPPINGS[`${section}:${source}`];
  const mappedFields = new Set(mapping?.rows.map((row) => row.to));
  const notProvided = (mapping?.fields ?? []).filter((field) => !mappedFields.has(field));
  const fallbackLine = (row: MappingRow) => {
    if (!row.fallback) return null;
    if (row.fallback === "batchType") {
      const used = plan?.batch_typed;
      return used === undefined
        ? t_i18n(MAPPING_NOTES.batchType)
        : t_i18n("{count, plural, =0 {The type you picked: used for no row} one {The type you picked: used for # row without a category} other {The type you picked: used for # rows without a category}}", { count: used });
    }
    return t_i18n(MAPPING_NOTES[row.fallback]);
  };

  const mappingStep = (
    <div className="space-y-3">
      <table className="w-full content-compact" data-testid="org-import.mapping">
        <thead>
          <tr className="text-left text-default-secondary">
            <th className="py-1 pr-3 font-medium">{t_i18n("In the profile")}</th>
            <th className="py-1 pr-3 font-medium">{t_i18n("In {platform}", { platform })}</th>
            <th className="py-1 font-medium">{t_i18n("Example")}</th>
          </tr>
        </thead>
        <tbody>
          {(mapping?.rows ?? []).map((row) => (
            <tr key={row.to} className="border-t border-elevation-subtle align-top">
              <td className="py-1.5 pr-3 text-default-primary">{t_i18n(PROFILE_FIELDS[row.to])}</td>
              <td className="py-1.5 pr-3 text-default-primary">
                {row.from.length > 0 && <code className="font-mono content-compact break-all">{row.from.join(" + ")}</code>}
                {row.note && <span className="block text-default-secondary">{t_i18n(MAPPING_NOTES[row.note])}</span>}
                {row.fallback && <span className="block text-default-secondary">{t_i18n("Fallback: {fallback}", { fallback: fallbackLine(row) ?? "" })}</span>}
              </td>
              <td className="max-w-40 truncate py-1.5 text-default-secondary" title={example(row.to)}>
                {preview.isPending ? <Spinner size="sm" label={t_i18n("Loading")} /> : example(row.to) || t_i18n("Empty")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {notProvided.length > 0 && (
        <p className="content-compact text-default-secondary" data-testid="org-import.not-provided">
          {t_i18n("Not provided by {platform}: {fields}. Rows keep what you typed for them.", { platform, fields: notProvided.map((field) => t_i18n(PROFILE_FIELDS[field])).join(", ") })}
        </p>
      )}
    </div>
  );

  // Counts are compact, the exact figure on hover (Design standards s.5): the plural reads the number, {shown} draws it.
  const totalLine = (count: number, kind: "read" | "match") => {
    const name = IMPORT_SOURCE_NAMES[plan?.source ?? ""] ?? plan?.source ?? "";
    const line = (shown: (value: number) => string) =>
      kind === "read"
        ? t_i18n("{count, plural, =0 {No entity read from {platform}} one {{shown} entity read from {platform}} other {{shown} entities read from {platform}}}", { count, shown: shown(count), platform: name })
        : t_i18n("{count, plural, =0 {No entity matches in {platform}} one {{shown} entity matches in {platform}} other {{shown} entities match in {platform}}}", { count, shown: shown(count), platform: name });
    return <CompactFigure className="whitespace-normal" exact={line(f.exactCount)}>{line(f.count)}</CompactFigure>;
  };
  const goneCount = plan?.counts.gone ?? 0;
  const previewStep = (
    <div className="space-y-3" data-testid="org-import.preview">
      {preview.isPending && (
        <p className="flex items-center gap-2 content-compact text-default-secondary" aria-live="polite">
          <Spinner size="sm" />
          {t_i18n("Reading {platform}", { platform })}
        </p>
      )}
      {preview.isError && <p className="content-compact text-feedback-error-primary">{t_i18n("The preview could not be made. Try again in a moment.")}</p>}
      {plan?.error && (
        <div className="space-y-1" data-testid="org-import.read-error">
          <p className="content-compact text-feedback-error-primary">
            {t_i18n("{platform} could not be read. Nothing will be saved.", { platform: IMPORT_SOURCE_NAMES[plan.source] ?? plan.source })}
          </p>
          {/* The cause and what to grant (Design standards s.5): only the reasons the backend words itself, never a platform's raw text. */}
          {plan.error_reason && <p className="content-compact text-default-secondary">{t_i18n("Reason: {reason}", { reason: plan.error_reason })}</p>}
        </div>
      )}
      {plan && !plan.error && (
        <>
          <p className="content-base text-default-primary" data-testid="org-import.source-total">
            {plan.source_total === null ? totalLine(plan.read, "read") : totalLine(plan.source_total, "match")}
          </p>
          {plan.truncated && <p className="content-compact text-default-secondary">{t_i18n("{count, plural, one {Only the first # entity is imported in one run; narrow the filters for the rest.} other {Only the first # entities are imported in one run; narrow the filters for the rest.}}", { count: plan.read })}</p>}
          <dl className="grid grid-cols-4 gap-2" data-testid="org-import.counts">
            {(Object.keys(OUTCOME_LABELS) as (keyof typeof OUTCOME_LABELS)[]).map((key) => (
              <div key={key} className="rounded-sm border border-elevation-subtle px-3 py-2">
                <dt className="content-compact text-default-secondary">{t_i18n(OUTCOME_LABELS[key])}</dt>
                <dd className="content-base font-semibold text-default-primary">
                  <CompactFigure exact={f.exactCount(plan.counts[key])}>{f.count(plan.counts[key])}</CompactFigure>
                </dd>
              </div>
            ))}
          </dl>
          <p className="content-compact text-default-secondary">
            {t_i18n("Updated: rows a previous import from this platform wrote. Skipped: a row typed by hand has the same name, and it is kept as typed.")}
          </p>
          {goneCount > 0 && (
            <SettingsSwitchField
              id="org-import-remove-gone"
              label={t_i18n("{count, plural, one {# row is no longer in {platform}} other {# rows are no longer in {platform}}}", { count: goneCount, platform })}
              helper={t_i18n("A previous import wrote them; this read with these filters no longer returns them. Kept unless you remove them.")}
              info={(plan.gone ?? []).slice(0, 10).join(", ")}
              checked={removeGone}
              onCheckedChange={setRemoveGone}
            />
          )}
          {plan.sample.length > 0 && (
            <ul className="max-h-40 space-y-0.5 overflow-y-auto content-compact text-default-primary" data-testid="org-import.sample">
              {plan.sample.map((row, index) => (
                <li key={index} className="truncate" title={String(row.name ?? row.product ?? "")}>
                  {String(row.name ?? row.product ?? "")}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      {plan && !plan.error && (
        <SettingsSwitchField
          id="org-import-keep-sync"
          label={t_i18n("Keep in sync")}
          helper={keepSync ? t_i18n("This import runs again on the schedule below, with the same filters and the same choice for the rows that left.") : t_i18n("Off: the import runs once. On: it also runs again on a schedule.")}
          info={t_i18n("A run saves a new profile version only when something changed, and never overwrites a row typed by hand. Edit, pause or delete it under Syncs on this page.")}
          checked={keepSync}
          onCheckedChange={setKeepSync}
        >
          {keepSync && <SyncScheduleFields idPrefix="org-import-sync" value={schedule} onChange={setSchedule} />}
        </SettingsSwitchField>
      )}
      {plan && !plan.error && <OrganizationSharedContextNotice elevation={1} actor={{ mode: keepSync ? "runs" : "reads", who: meLabel }} />}
      {apply.isError && <p className="content-compact text-feedback-error-primary">{t_i18n("The import was not saved. Preview again, then import.")}</p>}
      {createSync.isError && (
        <p className="content-compact text-feedback-error-primary" data-testid="org-import.sync-error">
          {apply.isSuccess ? t_i18n("The import was saved, but not the sync. Try Keep in sync again.") : t_i18n("The sync was not saved. Try again in a moment.")}
        </p>
      )}
    </div>
  );

  const content = { section: sectionStep, source: sourceStep, filters: filtersStep, mapping: mappingStep, preview: previewStep }[step];
  const importCount = (plan?.counts.new ?? 0) + (plan?.counts.updated ?? 0) + (removeGone ? goneCount : 0);

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{t_i18n("Import from the platforms")}</DialogTitle>
          <DialogDescription>{t_i18n("One section from one platform, no AI. Nothing is saved before Import, and rows typed by hand are kept.")}</DialogDescription>
        </DialogHeader>
        <DialogBody className="-m-1 space-y-4 p-1">
          <p className="content-compact font-medium text-default-secondary" data-testid="org-import.step">
            {t_i18n("Step {current} of {total}: {title}", { current: stepIndex + 1, total: STEPS.length, title: t_i18n(STEP_TITLES[step]) })}
          </p>
          {content}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={() => close(false)}>
            {t_i18n("Cancel")}
          </Button>
          {stepIndex > 0 && (
            <Button variant="secondary" onClick={() => setStep(STEPS[stepIndex - 1])}>
              {t_i18n("Back")}
            </Button>
          )}
          {step === "preview" ? (
            <Button
              onClick={runImport}
              disabled={!plan || Boolean(plan.error) || (importCount === 0 && !keepSync) || apply.isPending || createSync.isPending}
              aria-busy={apply.isPending || createSync.isPending || undefined}
            >
              {(apply.isPending || createSync.isPending) && <Spinner size="sm" tone="inherit" label={t_i18n("Saving...")} />}
              {keepSync
                ? t_i18n("{count, plural, =0 {Save the sync} one {Import {shown} row and keep in sync} other {Import {shown} rows and keep in sync}}", { count: importCount, shown: f.count(importCount) })
                : t_i18n("{count, plural, =0 {Nothing to import} one {Import {shown} row} other {Import {shown} rows}}", { count: importCount, shown: f.count(importCount) })}
            </Button>
          ) : (
            <Button onClick={next} disabled={!canNext}>
              {step === "mapping" ? t_i18n("Preview") : t_i18n("Next")}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
