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
  labelOf,
} from "@/lib/health";
import type { HealthDigest } from "@/types";
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
  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-3">
        {digest.specialist === "memory_steward" && (
          <Figure
            label={t_i18n("Consolidations")}
            value={counts(digest.consolidations, missing.has("consolidations"), t_i18n)}
          />
        )}
        {digest.specialist !== "memory_steward" && (
          <>
            <Figure label={t_i18n("Findings")} value={counts(digest.findings, missing.has("findings"), t_i18n)} />
            <Figure
              label={t_i18n("Experiments")}
              value={counts(digest.experiments, missing.has("experiments"), t_i18n)}
            />
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
          label={t_i18n("Budget spent")}
          value={t_i18n("{tokens} tokens · ${cost}", {
            tokens: n(digest.budget.spent_tokens),
            cost: Number(digest.budget.spent_cost_usd).toFixed(2),
          })}
        />
        {!missing.has("dry_run_writes") && digest.dry_run_writes !== null && digest.dry_run_writes > 0 && (
          <Figure label={t_i18n("Writes logged in dry-run, not executed")} value={n(digest.dry_run_writes)} />
        )}
      </div>
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
