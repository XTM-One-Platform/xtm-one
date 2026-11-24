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
 * One Platform Health cycle's digest (#8261). Every figure comes from the rows the
 * cycle left (`services/health/digest.py`): a section a lower level could
 * not give reads "Unavailable", never 0, and a status other than "Completed"
 * is shown as what it is, with what was skipped and why.
 */
import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { Badge } from "@/components/ui/badge";
import { useFormatter } from "@/i18n";
import {
  HEALTH_DIGEST_STATUS_LABELS,
  HEALTH_DIGEST_STATUS_VARIANTS,
  healthReasonLabel,
  healthSkipLabel,
  isModuleCheckerRole,
  labelOf,
} from "@/lib/health";
import { HEALTH_MODULE_LABELS, checkTitle } from "@/lib/health-modules";
import type { HealthDigest, HealthModule, HealthWeeklyModuleSummary } from "@/types";
import { useHealthName } from "@/hooks/api/useHealthApi";

function Figure({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-sm bg-elevation-default p-3">
      <p className="content-caption text-default-secondary">{label}</p>
      <p className="content-base font-mono-plex">{value}</p>
    </div>
  );
}

function counts(values: Record<string, number> | null, unavailable: boolean, t: (key: string) => string) {
  if (unavailable) return <span className="text-default-secondary">{t("Unavailable")}</span>;
  if (values === null) return "-";
  const entries = Object.entries(values).filter(([, n]) => n > 0);
  if (!entries.length) return "0";
  return entries.map(([key, n]) => `${t(healthReasonLabel(key))} ${n}`).join(" · ");
}

/** A Module Analyst cycle's recommendations: the total attached, then by confidence (`digest._recommendations`). */
function attached(values: Record<string, number> | null, unavailable: boolean, t: (key: string) => string) {
  if (unavailable || values === null) return counts(values, unavailable, t);
  const { attached: total = 0, ...byConfidence } = values;
  const detail = counts(byConfidence, false, t);
  return total > 0 ? `${total} · ${detail}` : "0";
}

/**
 * The weekly digest's Modules part (#101 S15e, dev-docs 13.8): per module, the check rounds of the
 * window, their verdict totals and the checks failed most, as the schedule computed them from the
 * stored reports (`digest.modules`); "Unavailable" when that read failed, never 0.
 */
function WeeklyModules({ modules, unavailable }: { modules: HealthDigest["modules"]; unavailable: boolean }) {
  const { t_i18n } = useFormatter();
  if (!unavailable && !modules) return null;
  const entries = Object.entries(modules ?? {}) as [HealthModule, HealthWeeklyModuleSummary][];
  return (
    <section className="space-y-2 rounded-sm bg-elevation-default p-3" aria-labelledby="health-digest-modules" data-testid="health-digest-modules">
      <h4 id="health-digest-modules" className="content-caption text-default-secondary">
        {t_i18n("Module checks")}
      </h4>
      {unavailable ? (
        <p className="content-compact text-default-secondary">{t_i18n("Unavailable")}</p>
      ) : !entries.length ? (
        <p className="content-compact text-default-secondary">{t_i18n("No check round since the last digest")}</p>
      ) : (
        <ul className="space-y-3">
          {entries.map(([module, part]) => (
            <li key={module} className="space-y-0.5" data-testid={`health-digest-module-${module}`}>
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="content-compact font-medium text-default-primary">{t_i18n(HEALTH_MODULE_LABELS[module] ?? module)}</span>
                <span className="tabular-nums content-caption text-default-secondary">
                  {t_i18n(
                    "{rounds, plural, one {# round} other {# rounds}} on {registrations, plural, one {# registration} other {# registrations}}",
                    { rounds: part.rounds, registrations: part.registrations },
                  )}
                </span>
              </div>
              <p className="tabular-nums content-compact text-default-primary">
                {t_i18n(
                  "{passed, plural, one {# passed} other {# passed}}, {warned, plural, one {# warning} other {# warnings}}, {failed, plural, one {# failed} other {# failed}}",
                  { passed: part.passed, warned: part.warned, failed: part.failed },
                )}
                {part.not_run ? `, ${t_i18n("{count, plural, one {# not run} other {# not run}}", { count: part.not_run })}` : ""}
              </p>
              {part.most_failed.length > 0 && (
                <div className="pt-1">
                  <p className="content-caption text-default-secondary">{t_i18n("Failed most")}</p>
                  <ul className="list-disc pl-5 content-compact text-default-primary">
                    {part.most_failed.map((check) => (
                      <li key={check.id}>
                        {checkTitle(`${module}.agentic.${check.id}`, t_i18n)}{" "}
                        <span className="tabular-nums text-default-secondary">
                          (
                          {t_i18n("{failed, plural, one {# failed} other {# failed}}, {warned, plural, one {# warning} other {# warnings}}", {
                            failed: check.failed,
                            warned: check.warned,
                          })}
                          )
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function DigestStatusBadge({ status }: { status: HealthDigest["status"] | "open" }) {
  const { t_i18n } = useFormatter();
  return (
    <Badge variant={HEALTH_DIGEST_STATUS_VARIANTS[status] ?? "outline"} className="shrink-0 whitespace-nowrap">
      {t_i18n(labelOf(HEALTH_DIGEST_STATUS_LABELS, status))}
    </Badge>
  );
}

export function CycleDigestView({ digest }: { digest: HealthDigest }) {
  const { t_i18n, intl } = useFormatter();
  const healthName = useHealthName();
  const n = (value: number) => intl.formatNumber(value);
  const missing = new Set(digest.unavailable);
  const proposals = digest.proposals;
  const moduleChecker = isModuleCheckerRole(digest.specialist);
  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-3">
        {digest.specialist === "memory_steward" && (
          <Figure
            label={t_i18n("Consolidations")}
            value={counts(digest.consolidations, missing.has("consolidations"), t_i18n)}
          />
        )}
        {moduleChecker && (
          <>
            <Figure
              label={t_i18n("Recommendations attached")}
              value={attached(digest.recommendations ?? null, missing.has("recommendations"), t_i18n)}
            />
            <Figure
              label={t_i18n("Agentic findings")}
              value={counts(digest.agentic_findings ?? null, missing.has("agentic_findings"), t_i18n)}
            />
          </>
        )}
        {digest.specialist !== "memory_steward" && (
          <>
            {!moduleChecker && (
              <>
                <Figure label={t_i18n("Findings")} value={counts(digest.findings, missing.has("findings"), t_i18n)} />
                <Figure
                  label={t_i18n("Experiments")}
                  value={counts(digest.experiments, missing.has("experiments"), t_i18n)}
                />
              </>
            )}
            <Figure
              label={t_i18n("Proposals")}
              value={
                missing.has("proposals") ? (
                  <span className="text-default-secondary">{t_i18n("Unavailable")}</span>
                ) : proposals ? (
                  t_i18n("{filed} filed · {applied} applied · {rolled} rolled back", {
                    filed: proposals.filed,
                    applied: proposals.applied,
                    rolled: proposals.rolled_back,
                  })
                ) : (
                  "-"
                )
              }
            />
          </>
        )}
        <Figure
          label={digest.budget.ledger === "module_analyst" ? t_i18n("Spent on the Module Analyst ceiling") : t_i18n("Budget spent")}
          value={t_i18n("{tokens} tokens · ${cost}", {
            tokens: n(digest.budget.spent_tokens),
            cost: Number(digest.budget.spent_cost_usd).toFixed(2),
          })}
        />
        {!missing.has("dry_run_writes") && digest.dry_run_writes !== null && digest.dry_run_writes > 0 && (
          <Figure label={t_i18n("Writes logged in dry-run, not executed")} value={n(digest.dry_run_writes)} />
        )}
      </div>
      <WeeklyModules modules={digest.modules} unavailable={missing.has("modules")} />
      {digest.skipped.length > 0 && (
        <div className="rounded-sm border border-alert-warning bg-alert-warning p-3">
          <p className="content-compact font-medium text-default-primary">{t_i18n("Skipped or not completed")}</p>
          <ul className="mt-1 list-disc pl-5 content-compact text-default-primary">
            {digest.skipped.map((skip, index) => (
              <li key={index}>
                {t_i18n(healthSkipLabel(skip.what))}: {t_i18n(healthReasonLabel(skip.reason))}
                {skip.count !== undefined && ` (${n(skip.count)})`}
              </li>
            ))}
          </ul>
        </div>
      )}
      {digest.narrative && (
        <div className="rounded-sm bg-elevation-default p-3">
          <p className="mb-1 content-caption text-default-secondary">{t_i18n("{name}'s report", { name: healthName })}</p>
          <ChatMarkdown content={digest.narrative} />
        </div>
      )}
    </div>
  );
}
