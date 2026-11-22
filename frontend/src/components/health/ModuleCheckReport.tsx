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
 * reason in the shared popover. Stored reports only (`GET /health/modules/{module}/checks`), no
 * model call: readable with AI off. Administrators only, as the card's AI block.
 */
import { Badge } from "@/components/ui/badge";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { InfoPopover } from "@/components/ui/info-popover";
import { useHealthModuleChecks } from "@/hooks/api/useHealthApi";
import { useFormatter } from "@/i18n";
import { HEALTH_MODULE_LABELS, checkTitle } from "@/lib/health-modules";
import type { HealthCheckVerdict, HealthModule, HealthModuleCheckEntry, HealthModuleCheckReport } from "@/types";

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

export interface CheckRoundTotals {
  passed: number;
  warned: number;
  failed: number;
  notRun: number;
  /** The newest round's end, ISO. */
  finishedAt: string;
  checks: HealthModuleCheckEntry[];
}

/** The latest round of every registration, summed: one module card, one line. Null when none ran. */
export function checkRoundTotals(reports: HealthModuleCheckReport[]): CheckRoundTotals | null {
  if (!reports.length) return null;
  const finishedAt = reports.map((report) => report.finished_at).reduce((a, b) => (Date.parse(b) > Date.parse(a) ? b : a));
  const checks = reports
    .flatMap((report) => report.checks)
    .sort((a, b) => VERDICT_ORDER.indexOf(a.verdict) - VERDICT_ORDER.indexOf(b.verdict));
  return {
    passed: reports.reduce((sum, report) => sum + report.passed, 0),
    warned: reports.reduce((sum, report) => sum + report.warned, 0),
    failed: reports.reduce((sum, report) => sum + report.failed, 0),
    notRun: reports.reduce((sum, report) => sum + report.not_run, 0),
    finishedAt,
    checks,
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

/** The agentic checks line of a module card: nothing while loading or on a read error. */
export function ModuleCheckReportLine({ module }: { module: HealthModule }) {
  const { t_i18n } = useFormatter();
  const { data } = useHealthModuleChecks(module);
  if (!data) return null;
  const totals = checkRoundTotals(data.items);
  return (
    <section className="space-y-1" aria-label={t_i18n("Agentic checks")} data-testid={`health-checks-${module}`}>
      <p className="content-compact font-medium text-default-secondary">{t_i18n("Agentic checks")}</p>
      {!totals ? (
        <p className="content-compact text-default-secondary">{t_i18n("No check round yet")}</p>
      ) : (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 content-compact text-default-secondary">
          <span className="tabular-nums text-default-primary" data-testid="health-checks-counts">
            {t_i18n(
              "{passed, plural, one {# passed} other {# passed}}, {warned, plural, one {# warning} other {# warnings}}, {failed, plural, one {# failed} other {# failed}}",
              { passed: totals.passed, warned: totals.warned, failed: totals.failed },
            )}
            {totals.notRun ? `, ${t_i18n("{count, plural, one {# not run} other {# not run}}", { count: totals.notRun })}` : ""}
          </span>
          <DateTimeCell value={totals.finishedAt} />
          {totals.checks.length ? (
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
              <ul className="max-h-80 space-y-2 overflow-y-auto">
                {totals.checks.map((check, index) => (
                  <CheckRow key={`${check.id}-${index}`} module={module} check={check} />
                ))}
              </ul>
            </InfoPopover>
          ) : null}
        </div>
      )}
    </section>
  );
}
