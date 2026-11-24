/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The agentic checks of an OpenCTI, OpenAEV or OpenCRQ card (#101 S15e, dev-docs/platform-health.md
 * section 13.6): the counts of the module checker's latest round on every registration (passed,
 * warnings, failed, not run), when it ran, and every check with its verdict, recommendation or
 * reason in the shared popover, grouped by registration when the module has several (UX round 1 B1).
 * Stored reports only (`GET /health/modules/{module}/checks`), no
 * model call: readable with AI off. Administrators only, as the card's AI block.
 */
import { Link } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { InfoPopover } from "@/components/ui/info-popover";
import { useHealthModuleChecks } from "@/hooks/api/useHealthApi";
import { usePlatformRegistrations } from "@/hooks/api/usePlatformApi";
import { HEALTH_RUN_AS_ANCHOR } from "@/pages/settings/HealthRunAsBlock";
import { HEALTH_SETTINGS_PATH } from "@/pages/settings/settings-routes";
import { useFormatter } from "@/i18n";
import { HEALTH_MODULE_LABELS, checkTitle } from "@/lib/health-modules";
import type { HealthCheckVerdict, HealthModule, HealthModuleCheckEntry, HealthModuleCheckReport } from "@/types";

/** The "Run checks as" block of Settings > Platform Health, by its own anchor (UX r1 B2). */
const HEALTH_RUN_AS_SETTINGS_PATH = `${HEALTH_SETTINGS_PATH}#${HEALTH_RUN_AS_ANCHOR}`;

/** A check's verdict in plain words. @i18n-keys */
const VERDICT_LABELS = {
  pass: "Passed",
  warn: "Warning",
  fail: "Failed",
  not_run: "Not run",
} as const satisfies Record<HealthCheckVerdict, string>;

const VERDICT_BADGE: Record<HealthCheckVerdict, "success" | "warning" | "destructive" | "outline"> = {
  pass: "success",
  warn: "warning",
  fail: "destructive",
  not_run: "outline",
};

/** Failed first, then warnings, not run, passed: what needs a look leads the list. */
const VERDICT_ORDER: HealthCheckVerdict[] = ["fail", "warn", "not_run", "pass"];

const verdictRank = (check: HealthModuleCheckEntry) => {
  const rank = VERDICT_ORDER.indexOf(check.verdict);
  return rank < 0 ? VERDICT_ORDER.indexOf("not_run") : rank;
};

/** One registration's latest round: its checks, failed first. */
interface CheckRoundGroup {
  registrationId: string;
  finishedAt: string;
  checks: HealthModuleCheckEntry[];
}

export interface CheckRoundTotals {
  passed: number;
  warned: number;
  failed: number;
  notRun: number;
  /** The newest round's end, ISO. */
  finishedAt: string;
  /** One group per registration, the one with the most severe check first. */
  groups: CheckRoundGroup[];
}

/** The latest round of every registration, summed: one module card, one line. Null when none ran. */
export function checkRoundTotals(reports: HealthModuleCheckReport[]): CheckRoundTotals | null {
  if (!reports.length) return null;
  const finishedAt = reports.map((report) => report.finished_at).reduce((a, b) => (Date.parse(b) > Date.parse(a) ? b : a));
  const groups = reports
    .map((report) => ({
      registrationId: report.registration_id,
      finishedAt: report.finished_at,
      checks: [...report.checks].sort((a, b) => verdictRank(a) - verdictRank(b)),
    }))
    .sort((a, b) => Math.min(...a.checks.map(verdictRank), VERDICT_ORDER.length) - Math.min(...b.checks.map(verdictRank), VERDICT_ORDER.length));
  return {
    passed: reports.reduce((sum, report) => sum + report.passed, 0),
    warned: reports.reduce((sum, report) => sum + report.warned, 0),
    failed: reports.reduce((sum, report) => sum + report.failed, 0),
    notRun: reports.reduce((sum, report) => sum + report.not_run, 0),
    finishedAt,
    groups,
  };
}

function CheckRow({ module, check }: { module: HealthModule; check: HealthModuleCheckEntry }) {
  const { t_i18n } = useFormatter();
  const verdict = VERDICT_LABELS[check.verdict] ? check.verdict : "not_run";
  const detail = verdict === "not_run" ? check.reason : check.recommendation;
  return (
    <li className="space-y-0.5" data-testid="health-check-row">
      <div className="flex min-w-0 items-center gap-2">
        <Badge variant={VERDICT_BADGE[verdict]} className="shrink-0 whitespace-nowrap">
          {t_i18n(VERDICT_LABELS[verdict])}
        </Badge>
        <span className="min-w-0 truncate content-compact text-default-primary">{checkTitle(`${module}.agentic.${check.id}`, t_i18n)}</span>
      </div>
      {detail && verdict !== "pass" ? <p className="whitespace-pre-line content-compact text-default-secondary">{detail}</p> : null}
    </li>
  );
}

/** The checks of one registration's round; the heading (name and round end) only when the module has several. */
function CheckGroup({ module, group, name }: { module: HealthModule; group: CheckRoundGroup; name: string | null }) {
  const { t_i18n } = useFormatter();
  const rows = group.checks.map((check, index) => <CheckRow key={`${check.id}-${index}`} module={module} check={check} />);
  if (name === null) return <>{rows}</>;
  return (
    <li className="space-y-1.5" data-testid="health-check-group">
      <div className="flex flex-wrap items-baseline justify-between gap-x-2 border-b border-elevation-subtle-soft pb-1">
        <h5 className="min-w-0 truncate content-compact font-medium text-default-primary">{name}</h5>
        <span className="inline-flex shrink-0 items-center gap-1 content-caption text-default-secondary">
          {t_i18n("As of")} <DateTimeCell value={group.finishedAt} />
        </span>
      </div>
      <ul className="space-y-2">{rows}</ul>
    </li>
  );
}

/** The card's one state when the checks have no run-as user with this module's access (dev-docs 12.3). */
function RunAsNeeded({ module }: { module: HealthModule }) {
  const { t_i18n } = useFormatter();
  return (
    <section className="space-y-1" aria-labelledby={`health-checks-heading-${module}`} data-testid={`health-checks-${module}`}>
      <h4 id={`health-checks-heading-${module}`} className="content-caption font-medium uppercase tracking-wider text-default-secondary">
        {t_i18n("Agentic checks")}
      </h4>
      <p className="content-compact text-default-primary" data-testid="health-checks-run-as">
        {t_i18n("Checks need a run-as user with {module} access.", { module: t_i18n(HEALTH_MODULE_LABELS[module]) })}{" "}
        <Link to={HEALTH_RUN_AS_SETTINGS_PATH} className="rounded-sm text-accent underline-offset-2 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-focus">
          {t_i18n("Set the run-as user")}
        </Link>
      </p>
    </section>
  );
}

/**
 * The agentic checks line of a module card: nothing while loading or on a read error. With `hideEmpty`
 * (the card's Last check round block already shows the round status), nothing either when no round
 * reported yet, so the card never says "No check round yet" twice (UX round 1 B1 on #1269).
 */
export function ModuleCheckReportLine({ module, hideEmpty = false }: { module: HealthModule; hideEmpty?: boolean }) {
  const { t_i18n } = useFormatter();
  const { data } = useHealthModuleChecks(module);
  const { data: registrations } = usePlatformRegistrations();
  if (!data) return null;
  const totals = checkRoundTotals(data.items);
  const grouped = (totals?.groups.length ?? 0) > 1;
  const nameOf = (registrationId: string) => {
    const registration = registrations?.find((row) => row.id === registrationId);
    return registration?.platform_title || registration?.platform_url || t_i18n(HEALTH_MODULE_LABELS[module]);
  };
  const hasChecks = totals?.groups.some((group) => group.checks.length > 0) ?? false;
  // No usable "Run checks as" user (dev-docs 12.3): one state and its setting, never a "not run" line per check.
  if (data.run_as === "not_set" || data.run_as === "no_access") return <RunAsNeeded module={module} />;
  if (!totals && hideEmpty) return null;
  return (
    <section className="space-y-1" aria-labelledby={`health-checks-heading-${module}`} data-testid={`health-checks-${module}`}>
      <h4 id={`health-checks-heading-${module}`} className="content-caption font-medium uppercase tracking-wider text-default-secondary">
        {t_i18n("Agentic checks")}
      </h4>
      {!totals ? (
        <p className="content-compact text-default-secondary">{t_i18n("No check round yet")}</p>
      ) : (
        <>
          <p className="tabular-nums content-compact text-default-primary" data-testid="health-checks-counts">
            {t_i18n(
              "{passed, plural, one {# passed} other {# passed}}, {warned, plural, one {# warning} other {# warnings}}, {failed, plural, one {# failed} other {# failed}}",
              { passed: totals.passed, warned: totals.warned, failed: totals.failed },
            )}
            {totals.notRun ? `, ${t_i18n("{count, plural, one {# not run} other {# not run}}", { count: totals.notRun })}` : ""}
          </p>
          <div className="flex flex-wrap items-center gap-x-2 content-caption text-default-secondary" data-testid="health-checks-meta">
            <span className="inline-flex items-center gap-1">
              {t_i18n("As of")} <DateTimeCell value={totals.finishedAt} />
            </span>
          {hasChecks ? (
            <InfoPopover
              label={t_i18n("Show the checks")}
              icon={t_i18n("Show the checks")}
              heading={t_i18n("Agentic checks")}
              headingClassName="content-compact"
              headingAside={<span className="content-compact text-default-secondary">{t_i18n(HEALTH_MODULE_LABELS[module])}</span>}
              width={384}
              triggerClassName="rounded-sm text-accent underline-offset-2 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-focus"
              popoverClassName="p-3"
              triggerTestId={`health-checks-expand-${module}`}
              popoverTestId={`health-checks-list-${module}`}
            >
              <ul className={grouped ? "max-h-80 space-y-3 overflow-y-auto" : "max-h-80 space-y-2 overflow-y-auto"}>
                {totals.groups.map((group) => (
                  <CheckGroup key={group.registrationId} module={module} group={group} name={grouped ? nameOf(group.registrationId) : null} />
                ))}
              </ul>
            </InfoPopover>
          ) : null}
          </div>
        </>
      )}
    </section>
  );
}
