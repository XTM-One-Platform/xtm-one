// SPDX-License-Identifier: Apache-2.0

/**
 * The icons of the CTEM case page (#1679 point 5): one shared map so a person,
 * an agent, a wait, an outcome, a state, a result, a mode, every section header
 * and every history event kind carry the same design-system icon everywhere,
 * plus the small actor and fact chips the decision record, the validation runs
 * and the History draw. Icons are decorative (aria-hidden): the label beside
 * them carries the meaning.
 */
import type * as React from "react";
import { Link as RouterLink } from "react-router-dom";
import { Icon, type IconName } from "@filigran/design-system";
import { type HotTone, ToneBadge } from "@/components/ui/tone-badge";
import { cn } from "@/lib/utils";

/** The icon of each case card header, by section. */
export const CASE_SECTION_ICON = {
  issues: "list-checks",
  history: "history",
  affected_assets: "server",
  analyst_review: "user",
  attack_path: "route",
  exploitation: "zap",
  opened_by: "signpost",
  remediation_decision: "scale",
  running_versions: "layers",
  treatment_plan: "wrench",
  validation: "shield-check",
} as const satisfies Record<string, IconName>;

/** The icon of each kind of fact a decision or an event carries. */
export const CASE_FACT_ICON = {
  person: "user",
  group: "users",
  agent: "bot",
  system: "settings",
  wait: "clock",
  deadline: "calendar",
  asked: "send",
  decided: "circle-check",
  then: "arrow-right",
  comment: "quote",
  channel: "message-square",
  decision: "external-link",
  state: "flag",
  meaning: "info",
  next: "signpost",
  safe: "shield-check",
  unsafe: "triangle-alert",
  order: "list-ordered",
} as const satisfies Record<string, IconName>;

/** The icon of each kind of case history event (`ctem_cases_store` kinds), a plain dot when unknown. */
const CASE_EVENT_ICON: Readonly<Record<string, IconName>> = {
  created: "circle-plus",
  issue_added: "plus",
  issue_closed: "circle-check",
  retargeted: "refresh-cw",
  state: "flag",
  owner: "user",
  due: "calendar",
  note: "sticky-note",
  split: "copy",
  merged: "layers-2",
  merge: "layers-2",
  signal: "radar",
  validation: "shield-check",
  sla_breached: "triangle-alert",
  exploitation: "zap",
  attack_path: "route",
  source_event: "custom/openaev",
  ticket: "tag",
  retest_requested: "rotate-ccw",
  gate: "badge-check",
  treatment_plan: "wrench",
  threat_package: "package-check",
  run: "bot",
};

/** The icon of a history event by its kind (`run` for a fleet run). */
export function caseEventIcon(kind: string): IconName {
  return CASE_EVENT_ICON[kind] ?? "circle";
}

/** The icon of an actor by its type, the system icon when unknown. */
export function actorIcon(type: string | null | undefined): IconName {
  if (type === "person" || type === "user") return CASE_FACT_ICON.person;
  if (type === "agent") return CASE_FACT_ICON.agent;
  if (type === "group" || type === "company" || type === "admins") return CASE_FACT_ICON.group;
  return CASE_FACT_ICON.system;
}

/** A decorative icon at the case page size. */
export function CaseIcon({ name, className }: { name: IconName; className?: string }) {
  return <Icon name={name} size={16} aria-hidden className={cn("shrink-0", className)} />;
}

/** A card title with its section icon before the label. */
export function CaseCardTitleText({ icon, children }: { icon: IconName; children: React.ReactNode }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-2">
      <CaseIcon name={icon} className="text-default-secondary" />
      <span className="min-w-0">{children}</span>
    </span>
  );
}

/**
 * A fact as a chip with its icon: a person, an agent, a wait, a mode. An agent
 * reads in the info tone (never the green link colour), a person and a plain
 * fact in the neutral tone; `to` makes the whole chip a link.
 */
export function CaseFactChip({
  icon,
  tone = "neutral",
  to,
  title,
  testId,
  children,
}: {
  icon: IconName;
  tone?: HotTone;
  to?: string;
  title?: string;
  testId?: string;
  children: React.ReactNode;
}) {
  const chip = (
    <ToneBadge tone={tone} icon={<CaseIcon name={icon} />} title={title} data-testid={testId} className="max-w-full">
      <span className="min-w-0 truncate">{children}</span>
    </ToneBadge>
  );
  if (!to) return chip;
  return (
    <RouterLink to={to} className="inline-flex max-w-full rounded-sm focus-visible:outline-2 focus-visible:outline-offset-2">
      {chip}
    </RouterLink>
  );
}
