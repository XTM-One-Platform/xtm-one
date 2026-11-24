// SPDX-License-Identifier: Apache-2.0

/**
 * The loop trace (dev-docs/ctem-loop-visibility.md section 4): where one
 * signal is in the exposure management loop, and why. A section of the Signals
 * page (#1263, `/ctem/signals?signal=<id>`), never a dialog, so a trace can be
 * shared and reopened after a reload.
 *
 * It draws the ten stages the object went through, the current one lit, the
 * stage where the loop stopped with its reason, and the stages not reached
 * greyed. It reads through the same API as the rows, so what the viewer
 * cannot read answers "This item is not available to you". The activity
 * stream refreshes the signal query, so a stage lights without a reload.
 */
import { type ReactNode } from "react";
import { Icon } from "@filigran/design-system";
import { useCtemSignal, useProposeSignalScope, type CtemDecisionRecord, type CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { Button } from "@/components/ui/button";
import { ModulePivotLink } from "@/components/ui/module-pivot-link";
import { SourceBadge } from "@/components/ui/source-badge";
import { useModuleBaseUrls } from "@/hooks/useModuleBaseUrls";
import { pivotModuleOf } from "@/lib/module-pivot";
import { useFormatter } from "@/i18n";
import { isNotFoundError } from "@/lib/api";
import { formatDayIntl, formatShortDateTimeIntl } from "@/lib/relative-time";
import { CTEM_PRODUCT_INFO, INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { cn, safeExternalUrl } from "@/lib/utils";
import { DecisionReason, decisionSentence, levelLabel } from "./DecisionReason";
import { EVIDENCE_HEALTH_ROW_KEYS, EVIDENCE_STATE_LABEL, evidenceHealthFacts, type EvidenceHealthFact } from "./evidence-health";
import { signalLoop, type LoopStageView } from "./loop-stages";

const LENS_LABEL: Record<string, string> = { inside: "Inside", outside: "Outside", world: "World" };

function StageGlyph({ status }: { status: LoopStageView["status"] }) {
  if (status === "passed") return <Icon name="check" size={16} className="text-feedback-success-primary" />;
  if (status === "stopped") return <Icon name="ban" size={16} className="text-feedback-warning-primary" />;
  // Parked is no fault (#1505): a neutral pause, never the warning tone of a stop.
  if (status === "parked") return <Icon name="minus" size={16} className="text-default-secondary" />;
  if (status === "current") return <Icon name="clock" size={16} className="text-accent" />;
  return <span className="h-1.5 w-1.5 rounded-full bg-elevation-hover ring-1 ring-[var(--border-elevation-subtle)]" aria-hidden />;
}

const STATUS_LABEL: Record<LoopStageView["status"], string> = {
  passed: "Passed",
  current: "Waiting",
  stopped: "Stopped",
  parked: "Parked",
  not_reached: "Not reached",
};

/**
 * The evidence keys that are the link to the source object, not a fact: `link`
 * (OpenCTI; an Aikido delivery carries no URL today) and `platform_url` (an OpenAEV delivery, CTEM-15-UI #956:
 * the scenario that regressed or the run that proved a path).
 */
const SOURCE_LINK_KEYS = ["link", "platform_url"] as const;

/** The link to the source object a signal's evidence carries, if any. */
export function signalSourceLink(evidence: Record<string, unknown> | null | undefined): string | undefined {
  for (const key of SOURCE_LINK_KEYS) {
    const value = evidence?.[key];
    if (typeof value === "string" && value) return value;
  }
  return undefined;
}

/** An evidence value in words: a scalar as it is, a list joined, anything else left out. */
function evidenceText(value: unknown): string {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value) && value.every((v) => typeof v === "string" || typeof v === "number")) return value.join(", ");
  return "";
}

/** One fact of the CTEM-08 trigger rules a signal's evidence carries, and the evidence key it says. */
type SignalRuleFact =
  | { key: "software"; software: string; matched: boolean; checked: boolean }
  | { key: "pir"; score: number };

/**
 * What the CTEM-08 trigger rules can say about a signal (CTEM-08-UI), read
 * from its evidence and its decision record, never claiming more than they
 * prove (#1000 UX round 1 B2):
 * - `software`: the source of the relationship. The listener writes it for
 *   every relationship to a vulnerability, before any software inventory match, so the
 *   rule "vulnerability on software we run" is named only when the scope gate
 *   resolved the signal to a software inventory product (`matched`); `checked` says the
 *   gate has read it at all.
 * - `pir`: the highest PIR score at delivery. The run gate reads the live score,
 *   so this is a fact about the PIR flag, never a claim that the rule fired.
 * Both facts are returned when the evidence carries both.
 */
export function signalRuleFacts(
  evidence: Record<string, unknown> | null | undefined,
  decision?: CtemDecisionRecord | null,
): SignalRuleFact[] {
  const facts: SignalRuleFact[] = [];
  const software = evidence?.software;
  const name = Array.isArray(software) ? software.find((s): s is string => typeof s === "string" && s.length > 0) : undefined;
  if (name) {
    const products = decision?.reconcile?.tech_stack;
    facts.push({ key: "software", software: name, matched: Array.isArray(products) && products.length > 0, checked: Boolean(decision) });
  }
  const pir = evidence?.pir;
  const scores = (Array.isArray(pir) ? pir : [])
    .map((p) => (p && typeof p === "object" ? (p as Record<string, unknown>).score_at_delivery : undefined))
    .filter((s): s is number => typeof s === "number");
  if (scores.length > 0) facts.push({ key: "pir", score: Math.max(...scores) });
  return facts;
}

/** The name of a signal's source: a CTEM product, else the integration's own name (Aikido Security), else its key. */
export function signalProviderName(provider: string): string {
  return CTEM_PRODUCT_INFO[provider]?.name ?? INTEGRATION_PROVIDER_NAMES[provider] ?? provider;
}

/** The Aikido finding types in words (CTEM-25-UI). @i18n-keys */
const AIKIDO_FINDING_TYPE = {
  open_source: "Dependency",
  sast: "Code",
  leaked_secret: "Leaked secret",
  iac: "Infrastructure as code",
  docker_container: "Container image",
  cloud: "Cloud",
  malware: "Malware",
  eol: "End of life",
  surface_monitoring: "Surface monitoring",
} as const;

/** The words of an Aikido finding type, undefined for a type the table does not name. */
function aikidoFindingTypeLabel(type: string): string | undefined {
  return (AIKIDO_FINDING_TYPE as Readonly<Record<string, string>>)[type];
}

/** The evidence keys an Aikido finding's rows say, left out of the raw evidence list. */
const AIKIDO_ROW_KEYS = [
  "type",
  "code_repo_name",
  "container_repo_name",
  "affected_package",
  "installed_version",
  "fixed_version",
  "sla_breached",
  "assets",
] as const;

/** One named fact of an Aikido finding (CTEM-25-UI). */
export type AikidoFact =
  | { key: "type"; type: string }
  | { key: "repository"; name: string }
  | { key: "image"; name: string }
  | { key: "package"; name: string; installed: string | null; fixed: string | null }
  | { key: "fixed"; fixed: string }
  | { key: "sla" };

function text(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number") return String(value);
  return null;
}

/**
 * What an Aikido signal's evidence says, in named facts (CTEM-25-UI): the
 * finding type, the repository or container image it sits on, the package
 * with its installed and fixed versions, the SLA breach. Read from the keys
 * the listener writes (backend ctem_signal_listeners._aikido_signal); a key
 * the delivery did not carry gives no row, and a signal from another source
 * gives none.
 */
export function aikidoFacts(provider: string, evidence: Record<string, unknown> | null | undefined): AikidoFact[] {
  if (provider !== "aikido" || !evidence) return [];
  const facts: AikidoFact[] = [];
  const type = text(evidence.type);
  if (type) facts.push({ key: "type", type });
  const repository = text(evidence.code_repo_name);
  if (repository) facts.push({ key: "repository", name: repository });
  const image = text(evidence.container_repo_name);
  if (image) facts.push({ key: "image", name: image });
  const pkg = text(evidence.affected_package);
  const fixed = text(evidence.fixed_version);
  if (pkg) facts.push({ key: "package", name: pkg, installed: text(evidence.installed_version), fixed });
  else if (fixed) facts.push({ key: "fixed", fixed });
  if (evidence.sla_breached === true) facts.push({ key: "sla" });
  return facts;
}

/** An Aikido fact as a row of the signal facts. */
function aikidoFactRow(fact: AikidoFact, t_i18n: ReturnType<typeof useFormatter>["t_i18n"]) {
  switch (fact.key) {
    case "type": {
      const words = aikidoFindingTypeLabel(fact.type);
      return { label: t_i18n("Finding type"), value: words ? t_i18n(words) : fact.type };
    }
    case "repository":
      return { label: t_i18n("Repository"), value: fact.name };
    case "image":
      return { label: t_i18n("Container image"), value: fact.name };
    case "package": {
      const installed = fact.installed ? `${fact.name} ${fact.installed}` : fact.name;
      return {
        label: t_i18n("Package"),
        value: fact.fixed ? t_i18n("{installed}, fixed in {fixed}", { installed, fixed: fact.fixed }) : installed,
      };
    }
    case "fixed":
      return { label: t_i18n("Fixed version"), value: fact.fixed };
    case "sla":
      return { label: t_i18n("SLA"), value: t_i18n("Breached in the source") };
  }
}

/** The OpenCTI observable types a leaked identity arrives as (CTEM-29-UI). @i18n-keys */
const IDENTITY_OBSERVED_AS = { "user-account": "User account", "email-addr": "Email address" } as const;

/** The OpenCTI objects that name a tracked organization (CTEM-29-UI). @i18n-keys */
const THIRD_PARTY_NAMED_IN = { report: "Report: {name}", incident: "Incident: {name}" } as const;

/** The relationship of a tracked organization, as Settings > Organizations words it. @i18n-keys */
const THIRD_PARTY_RELATIONSHIP = { provider: "Provider", customer: "Customer", partner: "Partner", subsidiary: "Subsidiary" } as const;

/** The evidence keys a leak or third-party signal's rows say, left out of the raw evidence list. */
const SUBJECT_ROW_KEYS = ["stix_type", "account_disabled", "report", "relationship", "organization_criticality"] as const;

/**
 * The dependency keys of a third-party signal the scope gate reads (the crown
 * jewels that depend on the organization, their OpenCRQ asset ids): Decision
 * inputs already say them, so the raw evidence list leaves them out.
 */
const THIRD_PARTY_SCOPE_KEYS = ["crown_jewels", "opencrq_asset_ids"] as const;

/** One named fact of a leaked identity or a third-party event (CTEM-29-UI). */
export type SubjectFact =
  | { key: "observed_as"; type: string }
  | { key: "account"; disabled: boolean }
  | { key: "fingerprint" }
  | { key: "named_in"; type: string; name: string }
  | { key: "relationship"; relationship: string }
  | { key: "organization_criticality"; criticality: string };

/**
 * What a credential-leak or a third-party signal's evidence says (CTEM-29-UI),
 * from the keys the listeners write (backend ctem_signal_listeners: the
 * identity listener and note_tracked_organizations): how the identity was
 * observed, whether the account is disabled, that a secret is kept only as its
 * fingerprint (`*_fingerprint`, never the value); the report or incident that
 * named the tracked organization, its relationship and criticality. Another
 * subject kind gives none.
 */
export function subjectFacts(subjectKind: string, evidence: Record<string, unknown> | null | undefined): SubjectFact[] {
  if (!evidence) return [];
  const facts: SubjectFact[] = [];
  if (subjectKind === "identity") {
    const type = text(evidence.stix_type);
    if (type) facts.push({ key: "observed_as", type });
    if (typeof evidence.account_disabled === "boolean") facts.push({ key: "account", disabled: evidence.account_disabled });
    if (Object.keys(evidence).some((k) => k.endsWith("_fingerprint"))) facts.push({ key: "fingerprint" });
  } else if (subjectKind === "third_party") {
    const type = text(evidence.stix_type);
    const name = text(evidence.report);
    if (type && name) facts.push({ key: "named_in", type, name });
    const relationship = text(evidence.relationship);
    if (relationship) facts.push({ key: "relationship", relationship });
    const criticality = text(evidence.organization_criticality);
    if (criticality) facts.push({ key: "organization_criticality", criticality });
  }
  return facts;
}

/** The evidence keys a set of subject facts already says. */
function subjectShownKeys(subjectKind: string, evidence: Record<string, unknown> | null | undefined): string[] {
  if (subjectKind !== "identity" && subjectKind !== "third_party") return [];
  const scopeKeys = subjectKind === "third_party" ? THIRD_PARTY_SCOPE_KEYS : [];
  return [...SUBJECT_ROW_KEYS, ...scopeKeys, ...Object.keys(evidence ?? {}).filter((k) => k.endsWith("_fingerprint"))];
}

/** A subject fact as a row of the signal facts. */
function subjectFactRow(fact: SubjectFact, t_i18n: ReturnType<typeof useFormatter>["t_i18n"]) {
  switch (fact.key) {
    case "observed_as": {
      const words = (IDENTITY_OBSERVED_AS as Readonly<Record<string, string>>)[fact.type];
      return { label: t_i18n("Observed as"), value: words ? t_i18n(words) : fact.type };
    }
    case "account":
      return { label: t_i18n("Account"), value: fact.disabled ? t_i18n("Disabled") : t_i18n("Enabled") };
    case "fingerprint":
      return { label: t_i18n("Secret"), value: t_i18n("Kept as a fingerprint only, never the value") };
    case "named_in": {
      const words = (THIRD_PARTY_NAMED_IN as Readonly<Record<string, string>>)[fact.type];
      return { label: t_i18n("Named in"), value: words ? t_i18n(words, { name: fact.name }) : fact.name };
    }
    case "relationship": {
      const words = (THIRD_PARTY_RELATIONSHIP as Readonly<Record<string, string>>)[fact.relationship];
      return { label: t_i18n("Relationship"), value: words ? t_i18n(words) : fact.relationship };
    }
    case "organization_criticality":
      return { label: t_i18n("Organization criticality"), value: String(levelLabel(fact.criticality, t_i18n)) };
  }
}

/**
 * The rows of an evidence health signal (CTEM-28-UI): the state in words, then
 * "No evidence since" with the date of the newest evidence, or "Never measured"
 * when there is none (a gap never reads as safe, never an empty dash), the risk
 * scenario, the evidence sources by name with when each last reported (left out
 * on a no_source state, which already says it), and for an emptied sync its row
 * counts and that its findings stay open.
 */
function evidenceHealthRows(fact: EvidenceHealthFact, t_i18n: ReturnType<typeof useFormatter>["t_i18n"], day: (iso: string) => string) {
  const rows: { label: string; value: string }[] = [
    { label: t_i18n("Evidence health"), value: t_i18n(EVIDENCE_STATE_LABEL[fact.state]) },
  ];
  if (fact.sync) {
    rows.push({
      label: t_i18n("Connector sync"),
      value: fact.sync.latest !== null && fact.sync.previous !== null
        ? t_i18n("{kind}: {latest, plural, one {# row} other {# rows}} after {previous}; its findings stay open", {
          kind: signalProviderName(fact.sync.kind),
          latest: fact.sync.latest,
          previous: fact.sync.previous,
        })
        : t_i18n("{kind}: emptied; its findings stay open", { kind: signalProviderName(fact.sync.kind) }),
    });
    if (fact.lastEvidence) rows.push({ label: t_i18n("Last sync"), value: day(fact.lastEvidence) });
    return rows;
  }
  rows.push({ label: t_i18n("No evidence since"), value: fact.lastEvidence ? day(fact.lastEvidence) : t_i18n("Never measured") });
  if (fact.scenario) rows.push({ label: t_i18n("Risk scenario"), value: fact.scenario });
  // A no_source state already says no source reports the asset: the row would repeat it.
  if (fact.state === "no_source" && fact.sources.length === 0) return rows;
  rows.push({
    label: t_i18n("Evidence sources"),
    value: fact.sources.length > 0
      ? fact.sources
        .map((s) => {
          const origin = signalProviderName(s.origin);
          return s.lastSeen ? t_i18n("{origin}, last reported {date}", { origin, date: day(s.lastSeen) }) : origin;
        })
        .join("; ")
      : t_i18n("No source reports this asset"),
  });
  return rows;
}

/**
 * The evidence keys the attack surface pass writes (backend
 * services/ctem_attack_surface.py, CTEM-26): the named rows below say them, so
 * the raw evidence list leaves them out. `reason` is the pass's English
 * sentence, already in the subject label.
 */
const SURFACE_ROW_KEYS = [
  "source", "provider", "hostnames", "ips", "ports", "dns_target", "cert_names", "reason",
  "baseline", "observed", "asset", "declared_internet_facing", "new_port", "previous_ports",
  "owned_domain", "address_ranges", "last_seen_at", "stale_after_days",
] as const;

/** The difference that fired an outside-lens signal: what the baseline held, what this pass saw (CTEM-26-UI). */
type SurfaceDiff =
  | { key: "new_port"; port: string; previous: string[] }
  | { key: "not_internet_facing"; ports: string[] }
  | { key: "dangling" }
  | { key: "outside_ranges"; domain: string; ip: string }
  | { key: "unseen"; lastSeen: string; days: number }
  | { key: "new_host"; ports: string[] };

/** One named fact of an outside-lens signal of the attack surface pass (CTEM-26-UI). */
export type SurfaceFact = {
  diff: SurfaceDiff | null;
  asset: string | null;
  ips: string[];
  ports: string[];
  dnsTarget: string | null;
  certNames: string[];
};

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string" && v.length > 0) : [];
}

/**
 * What an attack surface signal's evidence says (CTEM-26-UI), from the keys
 * `diff` writes for each difference: the port the previous pass did not see,
 * the declared asset that should not answer, the name that resolves to
 * nothing, the owned name pointing outside the owned ranges, the host unseen
 * for N days, the host neither declared nor seen before. A signal from
 * another source gives null.
 */
export function surfaceFacts(evidence: Record<string, unknown> | null | undefined): SurfaceFact | null {
  if (!evidence || evidence.source !== "attack_surface") return null;
  const ports = strings(evidence.ports);
  const newPort = text(evidence.new_port);
  const domain = text(evidence.owned_domain);
  const ips = strings(evidence.ips);
  const lastSeen = text(evidence.last_seen_at);
  const days = typeof evidence.stale_after_days === "number" ? evidence.stale_after_days : null;
  let diff: SurfaceDiff | null = null;
  if (newPort) diff = { key: "new_port", port: newPort, previous: strings(evidence.previous_ports) };
  else if (evidence.declared_internet_facing === "no") diff = { key: "not_internet_facing", ports };
  else if (evidence.observed === "no record") diff = { key: "dangling" };
  else if (domain && ips.length > 0) diff = { key: "outside_ranges", domain, ip: ips[0] };
  else if (lastSeen && days !== null) diff = { key: "unseen", lastSeen, days };
  else if (evidence.baseline === "absent") diff = { key: "new_host", ports };
  return {
    diff,
    asset: text(evidence.asset),
    ips,
    ports,
    dnsTarget: text(evidence.dns_target),
    certNames: strings(evidence.cert_names),
  };
}

/** The baseline and observed rows of a surface difference. */
function surfaceDiffRows(diff: SurfaceDiff, t_i18n: ReturnType<typeof useFormatter>["t_i18n"], day: (iso: string) => string) {
  const none = t_i18n("None");
  switch (diff.key) {
    case "new_port":
      return [
        { label: t_i18n("Baseline"), value: t_i18n("Previous pass saw {ports}", { ports: diff.previous.join(", ") || none }) },
        { label: t_i18n("Observed"), value: t_i18n("Answers on {port}, a new port", { port: diff.port }) },
      ];
    case "not_internet_facing":
      return [
        { label: t_i18n("Baseline"), value: t_i18n("Declared not internet-facing in the profile") },
        { label: t_i18n("Observed"), value: t_i18n("Answers from the Internet on {ports}", { ports: diff.ports.join(", ") || none }) },
      ];
    case "dangling":
      return [
        { label: t_i18n("Baseline"), value: t_i18n("The name is listed") },
        { label: t_i18n("Observed"), value: t_i18n("No DNS record backs it") },
      ];
    case "outside_ranges":
      return [
        { label: t_i18n("Baseline"), value: t_i18n("Names of {domain} point inside the owned address ranges", { domain: diff.domain }) },
        { label: t_i18n("Observed"), value: t_i18n("Points to {ip}, outside them", { ip: diff.ip }) },
      ];
    case "unseen":
      return [
        { label: t_i18n("Baseline"), value: t_i18n("The host last answered on {date}", { date: day(diff.lastSeen) }) },
        { label: t_i18n("Observed"), value: t_i18n("{count, plural, one {Unseen for at least # day} other {Unseen for at least # days}}", { count: diff.days }) },
      ];
    case "new_host":
      return [
        { label: t_i18n("Baseline"), value: t_i18n("Not declared and not seen by an earlier pass") },
        { label: t_i18n("Observed"), value: diff.ports.length > 0 ? t_i18n("Answers on {ports}", { ports: diff.ports.join(", ") }) : t_i18n("Answers") },
      ];
  }
}

/** The rows of an attack surface signal: the difference first, then what the pass saw of the host. */
function surfaceFactRows(fact: SurfaceFact, t_i18n: ReturnType<typeof useFormatter>["t_i18n"], day: (iso: string) => string) {
  const rows: { label: string; value: string }[] = [];
  if (fact.asset) rows.push({ label: t_i18n("Asset"), value: fact.asset });
  if (fact.diff) rows.push(...surfaceDiffRows(fact.diff, t_i18n, day));
  if (fact.ips.length > 0) rows.push({ label: t_i18n("Address"), value: fact.ips.join(", ") });
  if (fact.ports.length > 0 && fact.diff?.key !== "not_internet_facing" && fact.diff?.key !== "new_host") {
    rows.push({ label: t_i18n("Ports that answer"), value: fact.ports.join(", ") });
  }
  if (fact.dnsTarget) rows.push({ label: t_i18n("DNS target"), value: fact.dnsTarget });
  if (fact.certNames.length > 0) rows.push({ label: t_i18n("Certificate names"), value: fact.certNames.join(", ") });
  return rows;
}

/** The evidence keys an OpenAEV freshness fact says, left out of the raw evidence list. */
const FRESHNESS_ROW_KEYS = ["event", "resource_type"] as const;

/** One named fact of an OpenAEV asset freshness signal (CTEM-27-UI). */
type FreshnessFact = { key: "stale" };

/**
 * What an OpenAEV stale-asset signal says (CTEM-27-UI): the asset turned stale
 * in OpenAEV, unseen past the tenant's stale threshold (the provisioned event
 * `asset_stale`, backend openaev_trigger_provisioning, read by the path 6
 * listener). Another source or event gives none: a plain asset update is no
 * freshness verdict.
 */
export function freshnessFacts(provider: string, evidence: Record<string, unknown> | null | undefined): FreshnessFact[] {
  if (provider !== "openaev" || evidence?.event !== "asset_stale") return [];
  return [{ key: "stale" }];
}

/** A freshness fact as a row of the signal facts. */
function freshnessFactRow(t_i18n: ReturnType<typeof useFormatter>["t_i18n"]) {
  return { label: t_i18n("Freshness"), value: t_i18n("Stale: not seen by OpenAEV past the tenant's threshold") };
}

/** A rule fact as a row of the signal facts: a label that never says "triggered" unless the rule matched. */
function ruleFactRow(fact: SignalRuleFact, t_i18n: ReturnType<typeof useFormatter>["t_i18n"]) {
  if (fact.key === "pir") {
    return { label: t_i18n("PIR match"), value: t_i18n("Flagged in a PIR (score at delivery {score})", { score: fact.score }) };
  }
  if (fact.matched) {
    return { label: t_i18n("Trigger rule"), value: t_i18n("Vulnerability on software we run: {software}", { software: fact.software }) };
  }
  return {
    label: t_i18n("Source software"),
    value: fact.checked ? t_i18n("{software}, not in your software inventory", { software: fact.software }) : fact.software,
  };
}

/**
 * What the signal is, before where it is in the loop (CTEM-01-UI): its
 * subject, its source with the link to the source object, when it was first
 * and last seen, how many times, and the evidence the source gave.
 */
function SignalFacts({ signal }: { signal: CtemSignal }) {
  const { t_i18n, intl } = useFormatter();
  const link = safeExternalUrl(signalSourceLink(signal.evidence));
  const provider = signalProviderName(signal.source.provider);
  const moduleUrls = useModuleBaseUrls();
  // No link in the evidence: the OpenCTI entity the signal names, by its id (#1357); an OpenAEV
  // delivery carries its own platform_url, any other source has no module page.
  const pivotModule = pivotModuleOf(signal.source.provider) === "opencti" ? "opencti" : null;
  const ruleFacts = signalRuleFacts(signal.evidence, signal.decision);
  const findingFacts = aikidoFacts(signal.source.provider, signal.evidence);
  const identityFacts = subjectFacts(signal.subject.kind, signal.evidence);
  const health = evidenceHealthFacts(signal.evidence);
  const surface = surfaceFacts(signal.evidence);
  const staleFacts = freshnessFacts(signal.source.provider, signal.evidence);
  const shownKeys: string[] = [
    ...ruleFacts.map((fact) => fact.key),
    ...(findingFacts.length > 0 ? AIKIDO_ROW_KEYS : []),
    ...(staleFacts.length > 0 ? FRESHNESS_ROW_KEYS : []),
    ...subjectShownKeys(signal.subject.kind, signal.evidence),
    ...(health ? EVIDENCE_HEALTH_ROW_KEYS : []),
    ...(surface ? SURFACE_ROW_KEYS : []),
  ];
  const evidence = Object.entries(signal.evidence ?? {})
    .filter(([key]) => !(SOURCE_LINK_KEYS as readonly string[]).includes(key))
    .filter(([key]) => !shownKeys.includes(key))
    .map(([key, value]) => ({ key, value: evidenceText(value) }))
    .filter((row) => row.value)
    .slice(0, 8);
  const when = (iso: string | null) => (iso ? formatShortDateTimeIntl(intl, iso) : "--");
  const rows: { label: string; value: ReactNode }[] = [
    { label: t_i18n("Subject"), value: signal.subject.label || signal.subject.ref },
    {
      label: t_i18n("Source"),
      // The source's emblem and name (#1357), then the way to the source object.
      value: link ? (
        <a href={link} target="_blank" rel="noopener noreferrer" className="inline-flex max-w-full items-center gap-1 align-top text-accent hover:underline">
          <SourceBadge source={signal.source.provider} label={provider} testId="ctem-signal-source" />
          <Icon name="external-link" size={16} className="shrink-0" />
          <span className="sr-only">{t_i18n("Open the source")}</span>
        </a>
      ) : (
        // On the top of the line box, the 24 px pivot pulled into the 16 px line: the value stays on its
        // label's line and the row keeps its height (UX r1 B2 on #1358).
        <span className="inline-flex min-w-0 max-w-full items-center gap-1.5 align-top">
          <SourceBadge source={signal.source.provider} label={provider} testId="ctem-signal-source" />
          <ModulePivotLink
            className="-my-1"
            module={pivotModule}
            baseUrl={pivotModule ? moduleUrls[pivotModule] : null}
            id={signal.source.external_id}
            iconOnly
            testId="ctem-signal-source-pivot"
          />
        </span>
      ),
    },
    ...ruleFacts.map((fact) => ruleFactRow(fact, t_i18n)),
    ...findingFacts.map((fact) => aikidoFactRow(fact, t_i18n)),
    ...identityFacts.map((fact) => subjectFactRow(fact, t_i18n)),
    ...(health ? evidenceHealthRows(health, t_i18n, (iso) => formatDayIntl(intl, iso)) : []),
    ...(surface ? surfaceFactRows(surface, t_i18n, (iso) => formatDayIntl(intl, iso)) : []),
    ...staleFacts.map(() => freshnessFactRow(t_i18n)),
    { label: t_i18n("First seen"), value: when(signal.first_seen_at) },
    { label: t_i18n("Last seen"), value: when(signal.last_seen_at) },
    { label: t_i18n("Seen"), value: t_i18n("{count, plural, one {# time} other {# times}}", { count: signal.seen_count }) },
  ];
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 content-caption" data-testid="ctem-signal-facts">
      {rows.map((row) => (
        <div key={row.label} className="contents">
          <dt className="text-default-secondary">{row.label}</dt>
          <dd className="min-w-0 truncate">{row.value}</dd>
        </div>
      ))}
      {evidence.length > 0 && (
        <div className="contents">
          <dt className="text-default-secondary">{t_i18n("Evidence")}</dt>
          <dd className="min-w-0" data-testid="ctem-signal-evidence">
            {evidence.map((row) => (
              <span key={row.key} className="block truncate" title={row.value}>
                <span className="text-default-secondary">{row.key}: </span>
                {row.value}
              </span>
            ))}
          </dd>
        </div>
      )}
    </dl>
  );
}

/**
 * "Tune scope" on a watchlisted signal (CTEM-30, #937): one optional action
 * that stores a proposal to lower the scope threshold to this signal's
 * criticality. An administrator accepts or rejects it on the Exposure tab of
 * Settings > Organization (card "Proposed changes"); nothing waits on it and
 * the signal stays on the watchlist until then. The button names the change
 * (UX round 1 A3 on #941): the threshold it would set.
 */
function ProposeScope({ signal }: { signal: CtemSignal }) {
  const { t_i18n } = useFormatter();
  const propose = useProposeSignalScope();
  const criticality = signal.decision?.scope?.criticality;
  const level = String(levelLabel(criticality, t_i18n));
  if (signal.state !== "watchlist" || signal.decision?.reason_code !== "below_scope_threshold" || !criticality) return null;
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 content-caption" data-testid="ctem-signal-propose-scope">
      {propose.isSuccess ? (
        <span className="text-default-secondary" role="status">
          {t_i18n("Proposal saved. An administrator accepts or rejects it in Settings > Organization > Exposure.")}
        </span>
      ) : (
        <>
          <Button variant="secondary" size="sm" disabled={propose.isPending} onClick={() => propose.mutate(signal.id)}>
            {t_i18n("Propose scope threshold {level}", { level })}
          </Button>
          <span className="text-default-secondary">{t_i18n("Optional: nothing waits on it.")}</span>
        </>
      )}
      {propose.isError && <span className="text-feedback-error-primary" role="alert">{t_i18n("The proposal could not be saved")}</span>}
    </div>
  );
}

function SignalTrace({ signal }: { signal: CtemSignal }) {
  const { t_i18n, intl } = useFormatter();
  const stages = signalLoop(signal);
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 content-compact text-default-secondary">
        <span className="font-medium text-default-primary">{t_i18n(signal.trigger_path_label || signal.trigger_path)}</span>
        <span aria-hidden>·</span>
        <span>{t_i18n("Lens: {lens}", { lens: t_i18n(LENS_LABEL[signal.lens] ?? signal.lens) })}</span>
      </div>
      <SignalFacts signal={signal} />
      <DecisionReason state={signal.state} decision={signal.decision} showInputs />
      <ProposeScope signal={signal} />
      {/* One vertical list at every width (#851 UX round 1 B1): the dialog is
          896 px at most, so a row of ten cells (81 px each) wraps its labels
          and staggers the status captions; the list keeps one baseline. */}
      <ol className="flex flex-col gap-0" aria-label={t_i18n("Loop stages")} data-testid="ctem-loop-stages">
        {stages.map((stage, index) => (
          <li
            key={stage.key}
            data-stage={stage.key}
            data-status={stage.status}
            className={cn(
              "flex min-w-0 items-center gap-2 rounded-sm px-2 py-1.5",
              (stage.status === "current" || stage.status === "stopped" || stage.status === "parked") && "layer-1 bg-elevation-hover",
              stage.status === "not_reached" && "text-default-secondary",
            )}
          >
            <span className="flex h-4 w-4 shrink-0 items-center justify-center">
              <StageGlyph status={stage.status} />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block content-compact font-medium">
                <span className="sr-only">{t_i18n("Stage {n}", { n: index + 1 })}: </span>
                {t_i18n(stage.label)}
              </span>
              <span className="block content-caption text-default-secondary">
                {t_i18n(STATUS_LABEL[stage.status])}
                {stage.at ? ` · ${formatShortDateTimeIntl(intl, stage.at)}` : ""}
                {/* The stage where the loop stopped names why (#851 UX round 2 B4). */}
                {stage.status === "stopped" || stage.status === "parked"
                  ? ` · ${decisionSentence(signal.decision, t_i18n, (iso) => formatDayIntl(intl, iso))}`
                  : ""}
              </span>
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** The loop trace of one signal, read by id: a section of the Signals page (#1263). */
export function SignalTraceBody({ id }: { id: string }) {
  const { t_i18n } = useFormatter();
  const { data, error, isLoading } = useCtemSignal(id);
  if (isLoading) return <p className="content-compact text-default-secondary">{t_i18n("Loading...")}</p>;
  if (error && isNotFoundError(error)) return <p className="content-compact text-default-secondary">{t_i18n("This item is not available to you")}</p>;
  if (!data) return <p className="content-compact text-default-secondary">{t_i18n("This item is not available to you")}</p>;
  return <SignalTrace signal={data} />;
}

