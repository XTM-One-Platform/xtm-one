// SPDX-License-Identifier: Apache-2.0
/**
 * The mixed views of the Prioritization stage page (#11998, design round 5 approved):
 * under the funnel, the CVEs OpenCTI ranks first (CVSS, EPSS, known-exploited) and
 * OpenAEV's findings on the assets, split by whether they carry a CVE, and one gap
 * filler card per missing module, so no module combination leaves an empty page.
 *
 * Every figure comes from the stage's viz payload (backend `_stage_viz`); a list a
 * module did not answer is not drawn.
 */
import { useState, type ReactElement } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ModulePivotLink } from "@/components/ui/module-pivot-link";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { CtemStageViz } from "@/types";
import { HeroCard, HiddenChartValues } from "./stage-heroes";
import { StageGapFiller, type CtemModule } from "./DiscoveryMix";
import { severityLabelKey } from "./ctem-viz";

type TopVulnerability = NonNullable<CtemStageViz["top_vulnerabilities"]>[number];

/** Rows shown before "Show all (N)": five keep the card level with the Findings card beside it (UX round 1 of #12096, B2). */
const TOP_CVES_SHOWN = 5;

/**
 * The CVEs OpenCTI ranks first by CVSS, with their EPSS and known-exploited flag.
 * One grid on the list, each row a subgrid of it: every row shares the column
 * tracks and one row height, whatever a cell holds (UX round 1 of #12096, B1).
 */
function TopCves({ rows, openctiUrl }: { rows: TopVulnerability[]; openctiUrl?: string | null }) {
  const { t_i18n } = useFormatter();
  const { count, exactCount, percent } = useNumberFormat();
  const [expanded, setExpanded] = useState(false);
  const more = rows.length > TOP_CVES_SHOWN;
  const shown = expanded || !more ? rows : rows.slice(0, TOP_CVES_SHOWN);
  return (
    <div className="space-y-2">
      <ul
        className="grid grid-cols-[minmax(0,1fr)_auto_auto_3rem_1.5rem] gap-x-3 divide-y divide-[var(--border-elevation-subtle)] content-compact"
        data-testid="ctem-prioritization-top-cves-list"
      >
        {shown.map((v, i) => (
          <li key={`${v.name ?? "cve"}-${i}`} className="col-span-5 grid h-9 grid-cols-subgrid items-center">
            <span className="truncate font-medium" title={v.name ?? undefined}>
              {v.name}
            </span>
            <span className="tabular-nums text-default-secondary" title={v.cvss == null ? undefined : exactCount(v.cvss)}>
              {t_i18n("CVSS")} {count(v.cvss)}
              {v.severity ? ` · ${t_i18n(severityLabelKey(v.severity))}` : ""}
            </span>
            <span className="tabular-nums text-right text-default-secondary">
              {t_i18n("EPSS")} {percent(v.epss)}
            </span>
            <span className="text-right">{v.kev && <Badge variant="destructive">{t_i18n("KEV")}</Badge>}</span>
            {/* "Open in OpenCTI" (#1357): the cell stays, empty, when the id or the base URL is unknown, so the tracks hold. */}
            <span className="flex justify-end">
              <ModulePivotLink module="opencti" baseUrl={openctiUrl} id={v.id} iconOnly testId="ctem-top-cve-pivot" />
            </span>
          </li>
        ))}
      </ul>
      {more && (
        <Button type="button" variant="ghost" size="sm" className="h-9 text-accent" onClick={() => setExpanded((e) => !e)} aria-expanded={expanded}>
          {expanded ? t_i18n("Show fewer") : t_i18n("Show all ({count})", { count: count(rows.length) })}
        </Button>
      )}
    </div>
  );
}

/** Diameter of the findings donut: with the share line under it, the card's content stays within 24 px of the Top CVEs list beside it, five rows with or without Show all (UX rounds 2 and 3 of #12096, B2). */
const FINDINGS_DONUT = 176;
const FINDINGS_STROKE = 18;
/** The "other findings" arc and dot: the secondary text colour, legible on the card in both themes (UX round 2 of #12096, A4). */
const OTHER_FINDINGS_COLOR = "var(--text-default-secondary)";
const CVE_FINDINGS_COLOR = "var(--color-feedback-warning-primary)";

/** OpenAEV's open findings: a donut, with a CVE first, then the rest, the total in its centre. */
function FindingsSplit({ total, withCve }: { total: number; withCve: number }) {
  const { t_i18n } = useFormatter();
  const { count, exactCount, percent } = useNumberFormat();
  const other = Math.max(0, total - withCve);
  const cveShare = total > 0 ? Math.min(1, withCve / total) : 0;
  const r = (FINDINGS_DONUT - FINDINGS_STROKE) / 2;
  const c = 2 * Math.PI * r;
  const mid = FINDINGS_DONUT / 2;
  return (
    <div className="space-y-3" data-testid="ctem-prioritization-findings-split">
      <div className="flex flex-wrap items-center gap-6">
        <div className="relative shrink-0" style={{ width: FINDINGS_DONUT, height: FINDINGS_DONUT }}>
          <svg width={FINDINGS_DONUT} height={FINDINGS_DONUT} viewBox={`0 0 ${FINDINGS_DONUT} ${FINDINGS_DONUT}`} aria-hidden="true">
            <circle cx={mid} cy={mid} r={r} fill="none" stroke={total > 0 ? OTHER_FINDINGS_COLOR : "var(--border-elevation-subtle-soft)"} strokeWidth={FINDINGS_STROKE} />
            {total > 0 && withCve > 0 && (
              <circle
                cx={mid}
                cy={mid}
                r={r}
                fill="none"
                stroke={CVE_FINDINGS_COLOR}
                strokeWidth={FINDINGS_STROKE}
                strokeDasharray={`${cveShare * c} ${c}`}
                transform={`rotate(-90 ${mid} ${mid})`}
              />
            )}
          </svg>
          <div className="absolute inset-0 flex flex-col items-center justify-center text-center">
            <span className="text-2xl font-semibold text-default-primary tabular-nums" title={exactCount(total)}>
              {count(total)}
            </span>
            <span className="content-compact text-default-secondary">{t_i18n("{count, plural, one {finding} other {findings}}", { count: total })}</span>
          </div>
        </div>
        <dl className="grid min-w-0 flex-1 gap-3 content-compact">
          <div>
            <dt className="flex items-center gap-1.5 text-default-secondary">
              <span className="size-2.5 rounded-full" style={{ background: CVE_FINDINGS_COLOR }} aria-hidden="true" />
              {t_i18n("With a CVE")}
            </dt>
            <dd className="font-semibold tabular-nums" title={exactCount(withCve)}>
              {count(withCve)}
            </dd>
          </div>
          <div>
            <dt className="flex items-center gap-1.5 text-default-secondary">
              <span className="size-2.5 rounded-full" style={{ background: OTHER_FINDINGS_COLOR }} aria-hidden="true" />
              {t_i18n("Other findings")}
            </dt>
            <dd className="font-semibold tabular-nums" title={exactCount(other)}>
              {count(other)}
            </dd>
          </div>
        </dl>
      </div>
      <p className="content-compact text-default-secondary">
        {t_i18n("{count, plural, =0 {open findings on your assets} one {open finding on your assets} other {open findings on your assets}}", { count: total })}
        {total > 0 ? ` · ${t_i18n("{share} carry a CVE", { share: percent(cveShare) })}` : ""}
      </p>
    </div>
  );
}

/** The Prioritization cards under the funnel, then a gap filler per missing module. */
export function PrioritizationMix({
  viz,
  connected,
  onConnect,
  openctiUrl,
}: {
  viz: CtemStageViz | undefined;
  connected: { opencti: boolean; openaev: boolean; opencrq: boolean };
  onConnect: () => void;
  /** The connected OpenCTI's base URL: each top CVE then opens in OpenCTI (#1357). */
  openctiUrl?: string | null;
}) {
  const { t_i18n } = useFormatter();
  const { count, percent } = useNumberFormat();
  const topCves = (viz?.top_vulnerabilities ?? []).filter((v) => v.name);
  const findings = viz?.findings ?? null;
  const dataCards: ReactElement[] = [];
  const gapCards: ReactElement[] = [];

  if (connected.opencti && topCves.length > 0) {
    dataCards.push(
      <HeroCard key="top-cves" title={t_i18n("Top CVEs by CVSS")} testId="ctem-prioritization-top-cves">
        {/* The sr-only table goes first: as the last child of the space-y card it left a phantom 16 px margin under the caption (UX round 3 of #12096, B2). */}
        <HiddenChartValues
          caption={t_i18n("Top CVEs by CVSS")}
          head={[t_i18n("Vulnerability"), t_i18n("CVSS"), t_i18n("EPSS"), t_i18n("KEV")]}
          rows={topCves.map((v) => [v.name ?? "", count(v.cvss), percent(v.epss), v.kev ? t_i18n("KEV") : ""])}
        />
        <TopCves rows={topCves} openctiUrl={openctiUrl} />
        <p className="content-compact text-default-secondary">{t_i18n("The highest CVSS base scores in OpenCTI, with EPSS and known exploitation")}</p>
      </HeroCard>,
    );
  }
  if (connected.openaev && findings) {
    dataCards.push(
      <HeroCard key="findings" title={t_i18n("Findings on your assets")} testId="ctem-prioritization-findings">
        <FindingsSplit total={findings.total} withCve={findings.with_cve} />
        <p className="content-compact text-default-secondary">{t_i18n("Open findings OpenAEV reported on your assets")}</p>
      </HeroCard>,
    );
  }

  const copy: Record<CtemModule, { why: string; adds: string[] }> = {
    OpenCTI: {
      why: t_i18n("OpenCTI says which vulnerabilities are exploited in the wild."),
      adds: [t_i18n("The funnel from known CVEs to what needs action"), t_i18n("Top CVEs by CVSS, with EPSS and known exploitation")],
    },
    OpenAEV: {
      why: t_i18n("OpenAEV says which of these vulnerabilities are found on your assets."),
      adds: [t_i18n("Open findings on your assets, with and without a CVE"), t_i18n("The funnel narrowed to what your assets expose")],
    },
    OpenCRQ: {
      why: t_i18n("OpenCRQ says what fixing each exposure is worth."),
      adds: [t_i18n("Risk scenarios ranked by expected loss in EUR"), t_i18n("Share of the total expected loss per scenario")],
    },
  };
  const missing: CtemModule[] = [];
  if (!connected.opencti) missing.push("OpenCTI");
  if (!connected.openaev) missing.push("OpenAEV");
  if (!connected.opencrq) missing.push("OpenCRQ");
  for (const module of missing) {
    gapCards.push(
      <StageGapFiller
        key={module}
        module={module}
        why={copy[module].why}
        adds={copy[module].adds}
        testId={`ctem-prioritization-gap-${module.toLowerCase()}`}
        onConnect={onConnect}
      />,
    );
  }

  if (dataCards.length === 0 && gapCards.length === 0) return null;
  // Same layout rules as Discovery and Scoping (UX rounds of #12027 and #12076; equal
  // heights per row, Sam, 24 Nov 2026, #1612).
  return (
    <div className="space-y-5" data-testid="ctem-prioritization-mix">
      {dataCards.length > 0 && (
        <div
          className={cn("grid gap-5 [&>section]:h-full", dataCards.length > 1 && "2xl:grid-cols-2")}
          data-testid="ctem-prioritization-data-row"
          data-align="stretch"
          data-wide-columns={dataCards.length > 1 ? 2 : 1}
        >
          {dataCards}
        </div>
      )}
      {gapCards.length > 0 && (
        <div
          className={cn(
            "grid gap-5 [&>section]:h-full",
            gapCards.length >= 3 && "md:grid-cols-3",
            gapCards.length === 2 && "md:grid-cols-2",
          )}
          data-testid="ctem-prioritization-gap-row"
          data-columns={Math.min(gapCards.length, 3)}
        >
          {gapCards}
        </div>
      )}
    </div>
  );
}
