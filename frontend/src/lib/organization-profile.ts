// SPDX-License-Identifier: Apache-2.0

/**
 * The organization profile form (#8987): the stored profile as editable values
 * and back. Regions and PIRs are picked (lists of codes and ids); the numbers
 * are text until saved so an empty field stays empty, never 0.
 * The server validates again (ISO codes, bounds) and names the bad field.
 */

export interface ProfileLike {
  name: string;
  sector: string;
  sub_sector: string;
  regions: string[];
  size: string;
  crown_jewels: {
    name: string;
    business_service: string;
    owner: string;
    criticality: string;
    opencrq_asset_id: string;
    /** The assets and asset groups (by name) the crown jewel runs on (#11543, groups apart since #11781). */
    asset_names?: string[];
  }[];
  tech_stack: { product: string; vendor: string; cpe: string }[];
  ai_systems: { name: string; kind: string; owner: string; exposure: string; openaev_asset_id: string }[];
  pir_ids: string[];
  /** The last known OpenCTI name of each saved PIR (#9615): the page never labels a PIR with its id. */
  pir_names?: Record<string, string>;
  risk_appetite: { annual_loss_threshold: number | null; approval_threshold: number | null };
  loaded_hourly_rate: number | null;
  currency: string;
  /** The ticketing target; `sync` is how CTEM cases sync with it (#917), kept whole through the form. */
  ticketing: { integration: string; project: string; sync?: TicketingSync };
  /** Assets with their exposure (#11543); kept whole through the form. */
  assets_exposure?: AssetRow[];
  /** Groups the assets join through their `group` (#11781); kept whole through the form. */
  asset_groups?: AssetGroupRow[];
  /** The rules the CTEM prioritization funnel applies (#11543); kept whole through the form. */
  prioritization_policy?: PrioritizationPolicy;
  /** Knowledge bases of the organization's procedures (#11543); kept whole through the form. */
  enterprise_knowledge?: KnowledgeLink[];
  /** The posture score aimed for (#11547 S2e), set on the Governance & Risk page; kept whole through the form. */
  posture_target?: number | null;
}

/** An asset, typed on OpenAEV's taxonomy (#11781, lib/asset-taxonomy). */
export interface AssetRow {
  name: string;
  category: string;
  /** A subcategory of `category`; "" when none is chosen. */
  subcategory: string;
  criticality: string;
  environment: string;
  internet_facing: "yes" | "no" | "unknown";
  owner: string;
  group: string;
  tech_stack: string[];
  tags: string[];
  source: string;
  external_id: string;
}

/** A group of assets (OpenAEV's asset group, #11781). */
export interface AssetGroupRow {
  name: string;
  description: string;
  environment: string;
  internet_facing: "yes" | "no" | "unknown";
  owner: string;
  tech_stack: string[];
  tags: string[];
  source: string;
  external_id: string;
}

export interface PrioritizationPolicy {
  cvss_min: number | null;
  epss_min: number | null;
  kev_always_in: boolean;
  internet_exposure_required: boolean;
  min_crown_jewel_criticality: string;
  exclusions: { kind: string; value: string; reason: string }[];
  accepted_risks: { subject: string; scope: string; owner: string; expires_on: string; reason: string }[];
}

/** A case ticket status, as the backend stores it on `case.tickets` (ctem_case_loop.TICKET_STATUSES). */
export type CaseTicketStatus = "open" | "in_progress" | "done" | "cancelled";

/** How CTEM cases sync with the ticketing target (#917, backend TicketingSync, dev-docs/ctem-ticketing.md). */
export interface TicketingSync {
  create_on: "mobilize_p1_p2" | "mobilize_p1" | "manual";
  record_type: string;
  labels: string[];
  priority_map: Record<string, string>;
  status_map: Record<string, string>;
  done_starts_retest: boolean;
  on_reopen: "reopen" | "linked";
  due_date_from_sla: boolean;
}

/** The program defaults (doc 34 section 5), the backend's TicketingSync defaults. */
export const DEFAULT_TICKETING_SYNC: TicketingSync = {
  create_on: "mobilize_p1_p2",
  record_type: "",
  labels: [],
  priority_map: {},
  status_map: {},
  done_starts_retest: true,
  on_reopen: "reopen",
  due_date_from_sla: true,
};

/** The case severities a priority is mapped for. */
export const CASE_SEVERITIES = ["P1", "P2", "P3"] as const;
/** The case ticket statuses a system status can be mapped to. */
export const CASE_TICKET_STATUSES = ["open", "in_progress", "done", "cancelled"] as const satisfies readonly CaseTicketStatus[];

/**
 * The ServiceNow tables the CTEM case sync writes to (backend ctem_ticketing.target_problem); any other
 * table is refused with a 422 on save, so the form offers these alone.
 */
export const SERVICENOW_RECORD_TYPES = ["incident", "change_request", "problem"] as const;
export type ServiceNowRecordType = (typeof SERVICENOW_RECORD_TYPES)[number];

/** Whether a record type can be saved for this provider: free for Jira, one of the three tables for ServiceNow. */
export function isSupportedRecordType(provider: string, recordType: string): boolean {
  const value = recordType.trim();
  return provider !== "servicenow" || value === "" || (SERVICENOW_RECORD_TYPES as readonly string[]).includes(value);
}

/** The defaults the backend applies when a field is empty, per provider (ctem_ticketing.DEFAULT_*). */
export function ticketingDefaults(provider: string): { record_type: string; priority: Record<string, string> } {
  if (provider === "servicenow") return { record_type: "incident", priority: { P1: "1", P2: "2", P3: "3" } };
  return { record_type: "Task", priority: { P1: "Highest", P2: "High", P3: "Medium" } };
}

/** A stored sync (any field possibly missing on an older profile) with every field filled from the defaults. */
export function ticketingSyncOf(sync: Partial<TicketingSync> | undefined): TicketingSync {
  return {
    ...DEFAULT_TICKETING_SYNC,
    ...(sync ?? {}),
    labels: [...(sync?.labels ?? [])],
    priority_map: { ...(sync?.priority_map ?? {}) },
    status_map: { ...(sync?.status_map ?? {}) },
  };
}

/** The sync as saved: trimmed, blank priorities and status rows dropped, labels deduplicated. */
function cleanTicketingSync(sync: TicketingSync): TicketingSync {
  const priority_map: Record<string, string> = {};
  for (const [severity, value] of Object.entries(sync.priority_map)) if (value.trim()) priority_map[severity] = value.trim();
  const status_map: Record<string, string> = {};
  for (const [name, status] of Object.entries(sync.status_map)) if (name.trim() && status) status_map[name.trim()] = status;
  return { ...sync, record_type: sync.record_type.trim(), labels: splitList(sync.labels.join(",")), priority_map, status_map };
}

export interface KnowledgeLink {
  knowledge_base_id: string;
  label: string;
  kind: string;
}

export interface ProfileForm extends Omit<ProfileLike, "risk_appetite" | "loaded_hourly_rate"> {
  annual_loss_threshold: string;
  approval_threshold: string;
  loaded_hourly_rate: string;
}

/**
 * The criticality levels in words, shared by the crown jewels and the tracked
 * organizations. Criticality-specific keys: the bare "Medium" key is
 * translated in its time sense elsewhere (fr "Moyen terme"). @i18n-keys
 */
export const CRITICALITY_LABELS = {
  low: "Low (criticality)",
  medium: "Medium (criticality)",
  high: "High (criticality)",
  critical: "Critical (criticality)",
} as const;

/** The contract fields by path, with the words a person reads when one is missing. @i18n-keys */
export const FIELD_LABELS = {
  name: "Organization name",
  sector: "Sector",
  regions: "Regions of operation",
  size: "Organization size",
  crown_jewels: "Crown jewels and business services",
  tech_stack: "Tech stack",
  ai_systems: "AI systems inventory",
  pir_ids: "Priority intelligence requirements (PIRs)",
  "risk_appetite.annual_loss_threshold": "Risk tolerance per scenario",
  "risk_appetite.approval_threshold": "Escalation threshold",
  loaded_hourly_rate: "Loaded hourly rate",
  // Its own key: the shared "Currency" key is a number format in charts (fr "Monétaire").
  currency: "Currency of the amounts",
  ticketing: "Ticketing target",
} as const;

/**
 * The field contract of the form (#9173 S1c, UX B3): per field, the helper line
 * always visible under it and the bubble beside its label (what it is, the
 * unit, an example, who reads it, what happens when it is empty). The
 * readers named here are the seeded agents that read the field through
 * get_organization_context. @i18n-keys
 */
export const FIELD_HELP = {
  name: {
    helper: "The name agents use in briefs and reports.",
    info: "Your organization's legal or usual name. Read by every fleet in its reports. Empty: reports say \"your organization\".",
  },
  size: {
    helper: "Employees, as a band.",
    info: "Number of employees. Used to scale effort and loss estimates. Empty: agents name it as missing and do not scale.",
  },
  sector: {
    helper: "Your main industry, as threat reports name it.",
    info: "Example: Finance, Energy, Healthcare. Threat response and vulnerability triage rank a threat higher when it targets this sector. Empty: no sector relevance in rankings.",
  },
  sub_sector: {
    helper: "Optional: narrows the sector.",
    info: "Example: Retail banking. Refines the sector match in threat ranking. Empty: the sector alone is used.",
  },
  regions: {
    helper: "The countries where you operate.",
    info: "Pick each country. Threat response and AI security rank a threat higher when it targets these countries. Empty: no geographic relevance in rankings.",
  },
  pir_ids: {
    helper: "The OpenCTI priority intelligence requirements agents follow.",
    info: "Picked from the PIRs of your connected OpenCTI. Threat response scores a threat higher when it matches one. Empty: no PIR relevance in rankings.",
  },
  annual_loss_threshold: {
    helper: "Expected yearly loss per scenario that you accept without action.",
    info: "Unit: your currency per year. Example: 250,000. The risk quantifier compares each scenario's expected annual loss with it and ranks the ones above first. Empty: scenarios are priced but not compared.",
  },
  approval_threshold: {
    helper: "Expected yearly loss above which a fix needs approval.",
    info: "Unit: your currency per year. Example: 1,000,000. Above it, the risk quantifier flags the scenario for approval and the remediation coordinator escalates the fix. Empty: nothing is flagged for approval.",
  },
  loaded_hourly_rate: {
    helper: "The full cost of one hour of an analyst's time.",
    info: "Unit: your currency per hour, salary plus overheads. Example: 85. Turns the hours agents save into money in the ROI figures. Empty: ROI is shown in hours only.",
  },
  currency: {
    helper: "The currency of every amount on this page.",
    info: "ISO 4217 code. Used by the risk quantifier and the ROI figures. Empty: amounts have no currency and agents name it as missing.",
  },
  ticketing_integration: {
    helper: "Where the remediation coordinator opens tickets.",
    info: "One of your connected ticketing integrations. Empty: fixes are drafted as tickets but nowhere to file them.",
  },
  ticketing_project: {
    helper: "The project or queue tickets go to.",
    info: "Picked from the integration when it lists projects, typed otherwise. Example: SEC. Empty: the remediation coordinator asks where to file.",
  },
} as const;

/** The platform a drafted field came from (the draft's `source`), as the product is named. */
export const PLATFORM_NAMES: Record<string, string> = { opencti: "OpenCTI", openaev: "OpenAEV", opencrq: "OpenCRQ" };

/**
 * The platform an "unavailable" line of the draft names ("OpenCRQ: ..."), or ""
 * for any other line, so the page shows its own sentence and never the
 * backend's error text.
 */
export function unreadablePlatform(line: string): string {
  const head = line.split(":", 1)[0].trim();
  return head === "OpenCTI PIRs" || Object.values(PLATFORM_NAMES).includes(head) ? head : "";
}

export const SIZE_BANDS = ["1-50", "51-250", "251-1000", "1001-5000", "5001-20000", "20000+"] as const;

/**
 * ISO 4217 codes offered by the currency picker, the most used first; a
 * stored code outside the list is kept as an option.
 */
export const CURRENCIES = [
  "EUR", "USD", "GBP", "CHF", "JPY", "CAD", "AUD", "NZD", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON",
  "BGN", "TRY", "ILS", "AED", "SAR", "QAR", "INR", "SGD", "HKD", "CNY", "KRW", "TWD", "BRL", "MXN", "ZAR",
] as const;

/** Ticketing providers whose integration the ticketing picker offers, and the element type that lists their projects. */
export const TICKETING_PROJECT_ELEMENT: Record<string, string> = { jira: "jira_project", github: "repository", gitlab: "repository", servicenow: "" };

/** A number for display with thousands separators, in the person's locale; the raw text when it is not a number. */
export function formatAmount(text: string, locale?: string): string {
  const value = Number(text.trim().replace(/\s/g, ""));
  if (!text.trim() || !Number.isFinite(value)) return text;
  return new Intl.NumberFormat(locale).format(value);
}

const numberText = (value: number | null): string => (value === null || value === undefined ? "" : String(value));

export function splitList(text: string, upper = false): string[] {
  const items = text
    .split(/[,;\n]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => (upper ? item.toUpperCase() : item));
  return [...new Set(items)];
}

export function toForm(profile: ProfileLike): ProfileForm {
  return {
    name: profile.name,
    sector: profile.sector,
    sub_sector: profile.sub_sector,
    size: profile.size,
    regions: [...profile.regions],
    pir_ids: [...profile.pir_ids],
    pir_names: { ...(profile.pir_names ?? {}) },
    crown_jewels: profile.crown_jewels.map((jewel) => ({ ...jewel })),
    tech_stack: profile.tech_stack.map((entry) => ({ ...entry })),
    ai_systems: profile.ai_systems.map((system) => ({ ...system })),
    annual_loss_threshold: numberText(profile.risk_appetite.annual_loss_threshold),
    approval_threshold: numberText(profile.risk_appetite.approval_threshold),
    loaded_hourly_rate: numberText(profile.loaded_hourly_rate),
    currency: profile.currency,
    ticketing: { ...profile.ticketing, ...(profile.ticketing.sync ? { sync: ticketingSyncOf(profile.ticketing.sync) } : {}) },
    // The #11543 parts have their own editors: the form carries them unchanged so a save never drops them.
    ...(profile.assets_exposure ? { assets_exposure: profile.assets_exposure } : {}),
    ...(profile.asset_groups ? { asset_groups: profile.asset_groups } : {}),
    ...(profile.prioritization_policy ? { prioritization_policy: profile.prioritization_policy } : {}),
    ...(profile.enterprise_knowledge ? { enterprise_knowledge: profile.enterprise_knowledge } : {}),
    ...(profile.posture_target != null ? { posture_target: profile.posture_target } : {}),
  };
}

/** The names kept for *ids*: one per PIR followed that has a name, none for a PIR no longer followed. */
export function pirNamesOf(ids: string[], names: Record<string, string>): Record<string, string> {
  const kept: Record<string, string> = {};
  for (const id of ids) {
    const name = names[id.trim()]?.trim();
    if (name) kept[id.trim()] = name;
  }
  return kept;
}

/** The OpenCTI page of a PIR, or "" when the OpenCTI address is not known. */
export function pirLink(openctiUrl: string | undefined, id: string): string {
  return openctiUrl ? `${openctiUrl.replace(/\/+$/, "")}/dashboard/pirs/${encodeURIComponent(id)}` : "";
}

/** The form fields typed as amounts. */
export type AmountField = "annual_loss_threshold" | "approval_threshold" | "loaded_hourly_rate";

class InvalidAmount extends Error {
  readonly field: AmountField;
  constructor(field: AmountField, label: string) {
    super(label);
    this.field = field;
  }
}

/** A number typed in the form: empty is null, anything else must be a non-negative number. */
function parseAmount(text: string, label: string, field: AmountField): number | null {
  const trimmed = text.trim().replace(/\s/g, "");
  if (!trimmed) return null;
  const value = Number(trimmed);
  if (!Number.isFinite(value) || value < 0) throw new InvalidAmount(field, label);
  return value;
}

/**
 * The form as the profile the API saves, or the English label and the form
 * field of the first field that is not a valid amount, so the form marks that
 * field invalid and focuses it (rows with no name are dropped).
 */
export function fromForm(form: ProfileForm): { profile: ProfileLike } | { invalid: string; field: AmountField } {
  try {
    return {
      profile: {
        name: form.name.trim(),
        sector: form.sector.trim(),
        sub_sector: form.sub_sector.trim(),
        size: form.size,
        regions: [...new Set(form.regions.map((code) => code.trim().toUpperCase()).filter(Boolean))],
        pir_ids: [...new Set(form.pir_ids.map((id) => id.trim()).filter(Boolean))],
        pir_names: pirNamesOf(form.pir_ids, form.pir_names ?? {}),
        crown_jewels: form.crown_jewels.filter((jewel) => jewel.name.trim()),
        tech_stack: form.tech_stack.filter((entry) => entry.product.trim()),
        ai_systems: form.ai_systems.filter((system) => system.name.trim()),
        risk_appetite: {
          annual_loss_threshold: parseAmount(form.annual_loss_threshold, FIELD_LABELS["risk_appetite.annual_loss_threshold"], "annual_loss_threshold"),
          approval_threshold: parseAmount(form.approval_threshold, FIELD_LABELS["risk_appetite.approval_threshold"], "approval_threshold"),
        },
        loaded_hourly_rate: parseAmount(form.loaded_hourly_rate, FIELD_LABELS.loaded_hourly_rate, "loaded_hourly_rate"),
        currency: form.currency.trim().toUpperCase(),
        ticketing: {
          integration: form.ticketing.integration.trim(),
          project: form.ticketing.project.trim(),
          // The sync settings travel with the target: a save never resets them to the defaults (#917).
          ...(form.ticketing.sync ? { sync: cleanTicketingSync(form.ticketing.sync) } : {}),
        },
        // Rows left blank in the editors are dropped, as the crown jewels' are (#11543 S3).
        ...(form.assets_exposure ? { assets_exposure: form.assets_exposure.filter((asset) => asset.name.trim()) } : {}),
        ...(form.asset_groups ? { asset_groups: form.asset_groups.filter((group) => group.name.trim()) } : {}),
        ...(form.prioritization_policy
          ? {
              prioritization_policy: {
                ...form.prioritization_policy,
                accepted_risks: form.prioritization_policy.accepted_risks.filter((risk) => risk.subject.trim()),
              },
            }
          : {}),
        ...(form.enterprise_knowledge ? { enterprise_knowledge: form.enterprise_knowledge.filter((link) => link.knowledge_base_id) } : {}),
        ...(form.posture_target != null ? { posture_target: form.posture_target } : {}),
      },
    };
  } catch (err) {
    if (err instanceof InvalidAmount) return { invalid: err.message, field: err.field };
    throw err;
  }
}
