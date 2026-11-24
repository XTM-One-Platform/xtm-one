// SPDX-License-Identifier: Apache-2.0

/**
 * The CTEM Governance & Risk page (/ctem/governance, dev-docs/ctem-stage-viz.md
 * section 3.6, slice S2e of #11547): the posture against the target the
 * organization set, drawn on the five band zones over 90 days with the gap
 * shaded; the accepted risks of the prioritization policy by days until they
 * expire; and the SLA adherence OpenCRQ will feed, stated as not configured.
 *
 * Every mark comes from the posture history or the organization profile; a
 * missing value draws the dashed not-configured state with one way to fill it.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import { useElementWidth } from "@/components/custom-dashboard/charts/common";
import { useAuth } from "@/hooks/useAuth";
import { useNow } from "@/hooks/useTick";
import { useMe } from "@/hooks/api/useAuthApi";
import { useCtemPosture, useCtemPostureHistory } from "@/hooks/api/useCtemMetricsApi";
import { useCtemLoopMeasures } from "@/hooks/api/useCtemCasesApi";
import { useOrganizationContext, usePatchOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { CTEM_BAND_CONFIG, CTEM_PRODUCT_INFO } from "@/lib/constants";
import { profileFieldHref } from "@/lib/organization-profile-tabs";
import { safeExternalUrl } from "@/lib/utils";
import type { CtemVertical } from "@/types";
import { CasesInPhaseCard } from "./ctem/CasesInPhaseCard";
import { CtemPageHeader } from "./ctem/CtemPageHeader";
import { HeroCard, HiddenChartValues, NotConfiguredFrame, useRovingMarks } from "./ctem/stage-heroes";
import { formatTrendDay, historyWindow } from "./ctem/trend-window";
import {
  BAND_ZONES, GOVERNANCE_DAYS, RISK_HORIZON_DAYS, postureGap, risksByExpiry, stripPosition, type RiskOnStrip,
} from "./ctem/governance";

const DANGER = "var(--color-feedback-error-primary)";
const WARNING = "var(--color-feedback-warning-primary)";
const ACCENT = "var(--color-accent)";
const CHART_H = 220;
const PAD = { top: 12, right: 72, bottom: 24, left: 32 };

/** The posture line on the band zones, the dashed target and the gap shaded below it. */
function PostureVsTarget({
  days,
  target,
}: {
  days: { label: string; score: number | null }[];
  target: number | null;
}) {
  const { t_i18n } = useFormatter();
  const { ref, width } = useElementWidth<HTMLDivElement>();
  const roving = useRovingMarks(days.length);
  const w = Math.max(width, 320);
  const innerW = w - PAD.left - PAD.right;
  const innerH = CHART_H - PAD.top - PAD.bottom;
  const x = (i: number) => PAD.left + (days.length <= 1 ? innerW / 2 : (i * innerW) / (days.length - 1));
  const y = (v: number) => PAD.top + innerH * (1 - Math.max(0, Math.min(100, v)) / 100);
  const recorded = days.map((d, i) => ({ ...d, i })).filter((d): d is { label: string; score: number; i: number } => d.score != null);
  const line = recorded.map((d, k) => `${k === 0 ? "M" : "L"}${x(d.i)},${y(d.score)}`).join(" ");
  // The gap: between the target and the posture where the posture is under it.
  const gapArea =
    target != null && recorded.length > 1
      ? `M${x(recorded[0].i)},${y(target)} ` +
        recorded.map((d) => `L${x(d.i)},${y(Math.min(d.score, target))}`).join(" ") +
        ` L${x(recorded[recorded.length - 1].i)},${y(target)} Z`
      : null;
  const active = days[roving.active];

  return (
    <div ref={ref} className="w-full">
      <svg
        width={w}
        height={CHART_H}
        viewBox={`0 0 ${w} ${CHART_H}`}
        role="group"
        aria-label={t_i18n("Posture against target")}
        tabIndex={days.length > 0 ? 0 : -1}
        onKeyDown={(e) => roving.onKeyDown(e)}
        className="max-w-full focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)]"
        data-testid="ctem-governance-chart"
      >
        {BAND_ZONES.map((zone) => (
          <g key={zone.band} data-testid={`ctem-governance-zone-${zone.band}`}>
            <rect
              x={PAD.left}
              y={y(zone.to)}
              width={innerW}
              height={y(zone.from) - y(zone.to)}
              fill={CTEM_BAND_CONFIG[zone.band].ring}
              opacity={0.08}
            />
            <text x={PAD.left + innerW + 6} y={(y(zone.from) + y(zone.to)) / 2} dominantBaseline="middle" fontSize={11} fill="currentColor">
              {t_i18n(CTEM_BAND_CONFIG[zone.band].label)}
            </text>
          </g>
        ))}
        {[0, 50, 100].map((tick) => (
          <text key={tick} x={PAD.left - 6} y={y(tick)} textAnchor="end" dominantBaseline="middle" fontSize={11} fill="currentColor">
            {tick}
          </text>
        ))}
        {gapArea && <path d={gapArea} fill={DANGER} opacity={0.18} data-testid="ctem-governance-gap-area" />}
        {target != null && (
          <g data-testid="ctem-governance-target-line">
            <line x1={PAD.left} x2={PAD.left + innerW} y1={y(target)} y2={y(target)} stroke="currentColor" strokeWidth={1.5} strokeDasharray="6 4" />
            <text x={PAD.left + 4} y={y(target) - 6} fontSize={11} fill="currentColor">
              {t_i18n("Target {value}", { value: target })}
            </text>
          </g>
        )}
        {line && <path d={line} fill="none" stroke={ACCENT} strokeWidth={2} data-testid="ctem-governance-posture-line" />}
        {recorded.map((d) => (
          <circle
            key={d.i}
            cx={x(d.i)}
            cy={y(d.score)}
            r={roving.active === d.i ? 5 : 2.5}
            fill={ACCENT}
            role="img"
            aria-label={t_i18n("{day}: posture {score}", { day: d.label, score: Math.round(d.score) })}
          >
            <title>{t_i18n("{day}: posture {score}", { day: d.label, score: Math.round(d.score) })}</title>
          </circle>
        ))}
        {days.length > 1 && (
          <>
            <text x={x(0)} y={CHART_H - 6} fontSize={11} fill="currentColor">{days[0].label}</text>
            <text x={x(days.length - 1)} y={CHART_H - 6} textAnchor="end" fontSize={11} fill="currentColor">
              {days[days.length - 1].label}
            </text>
          </>
        )}
      </svg>
      {active && active.score != null && (
        <p className="sr-only" aria-live="polite">
          {t_i18n("{day}: posture {score}", { day: active.label, score: Math.round(active.score) })}
        </p>
      )}
    </div>
  );
}

/** The accepted risks on one strip, by days until expiry; the expired ones first, in red. */
function RiskStrip({ risks }: { risks: RiskOnStrip[] }) {
  const { t_i18n } = useFormatter();
  const roving = useRovingMarks(risks.length);
  const dayLabel = (r: RiskOnStrip) =>
    r.expired
      ? t_i18n("Expired {count, plural, one {# day} other {# days}} ago", { count: -r.days })
      : r.days === 0
        ? t_i18n("Expires today")
        : t_i18n("Expires in {count, plural, one {# day} other {# days}}", { count: r.days });
  return (
    <div className="space-y-3">
      <div
        className="relative h-10 rounded-sm border border-elevation-subtle-soft bg-elevation-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)]"
        role="group"
        aria-label={t_i18n("Accepted risks by days until expiry")}
        tabIndex={0}
        onKeyDown={(e) => roving.onKeyDown(e)}
        data-testid="ctem-governance-risk-strip"
      >
        {risks.map((r, i) => (
          <span
            key={`${r.subject}-${i}`}
            role="img"
            aria-label={`${r.subject}: ${dayLabel(r)}`}
            title={`${r.subject}: ${dayLabel(r)}`}
            className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-elevation-default"
            style={{
              left: `calc(8px + (100% - 16px) * ${stripPosition(r.days)})`,
              width: roving.active === i ? 16 : 12,
              height: roving.active === i ? 16 : 12,
              background: r.expired ? DANGER : r.days <= 30 ? WARNING : ACCENT,
            }}
            data-testid={`ctem-governance-risk-${i}`}
          />
        ))}
      </div>
      <div className="flex justify-between content-caption text-default-secondary">
        <span>{t_i18n("Today")}</span>
        <span>{t_i18n("{count, plural, one {# day} other {# days}}", { count: RISK_HORIZON_DAYS })}</span>
      </div>
      <ul className="space-y-1.5" data-testid="ctem-governance-risk-list">
        {risks.slice(0, 5).map((r, i) => (
          <li key={`${r.subject}-${i}`} className="flex items-baseline justify-between gap-3 content-compact">
            <span className="truncate font-medium" title={r.subject}>{r.subject}</span>
            <span className={r.expired ? "shrink-0 text-feedback-error-primary" : "shrink-0 text-default-secondary"}>{dayLabel(r)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * The target typed by an administrator: a whole score from 0 to 100. The label
 * sits above the field, an out-of-range value says why under it, and Cancel
 * leaves the edit state without saving.
 */
function TargetForm({
  initial,
  onSave,
  onCancel,
  pending,
}: {
  initial: number | null;
  onSave: (value: number) => void;
  onCancel: () => void;
  pending: boolean;
}) {
  const { t_i18n } = useFormatter();
  const [text, setText] = useState(initial == null ? "" : String(initial));
  const value = Number(text);
  const valid = text.trim() !== "" && Number.isInteger(value) && value >= 0 && value <= 100;
  const invalid = text.trim() !== "" && !valid;
  return (
    <form
      className="flex flex-wrap items-start gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid) onSave(value);
      }}
      data-testid="ctem-governance-target-form"
    >
      <SettingsField
        id={TARGET_FIELD}
        label={t_i18n("Posture target")}
        helper={t_i18n("A score from 0 to 100")}
        error={invalid ? t_i18n("Enter a whole number from 0 to 100") : undefined}
        className="w-48"
      >
        <Input
          id={TARGET_FIELD}
          inputMode="numeric"
          value={text}
          aria-invalid={invalid}
          aria-labelledby={settingsFieldLabelId(TARGET_FIELD)}
          aria-describedby={settingsFieldHintId(TARGET_FIELD)}
          onChange={(e) => setText(e.target.value)}
        />
      </SettingsField>
      {/* The SettingsField label line plus its gap is 20 px (pt-5): the buttons start at the field's top (measured in the lab, UX round 2). */}
      <div className="flex gap-2 pt-5" data-testid="ctem-governance-target-actions">
        <Button type="submit" disabled={!valid || pending}>
          {t_i18n("Save")}
        </Button>
        <Button type="button" variant="outline" onClick={onCancel} data-testid="ctem-governance-target-cancel">
          {t_i18n("Cancel")}
        </Button>
      </div>
    </form>
  );
}

const TARGET_FIELD = "ctem-posture-target";

const LOOP_STEPS = [
  { key: "signal_to_case", label: "Signal to case" },
  { key: "case_to_validation", label: "Case to first validation" },
  { key: "case_to_decision", label: "Case to decision" },
  { key: "case_to_passing_retest", label: "Case to passing re-test" },
] as const;

/**
 * How fast the loop runs (CTEM-30): the median of each step over the cases the
 * reader sees, read from the case history, and the share of resolved cases
 * closed on a passing validation. A step no case reached reads "No case yet",
 * never 0.
 */
function LoopMeasures() {
  const { t_i18n } = useFormatter();
  const { data, isError } = useCtemLoopMeasures();
  const m = data?.measures;
  const duration = (hours: number | null) =>
    hours == null
      ? t_i18n("No case yet")
      : hours < 1
        ? t_i18n("Under 1 hour")
        : hours < 48
        ? t_i18n("{count, plural, one {# hour} other {# hours}}", { count: Math.round(hours * 10) / 10 })
        : t_i18n("{count, plural, one {# day} other {# days}}", { count: Math.round(hours / 2.4) / 10 });
  if (isError) return <NotConfiguredFrame reason={t_i18n("The loop measures could not be read")} testId="ctem-governance-loop-error" />;
  if (!m) return null;
  if (m.case_count === 0) {
    return <NotConfiguredFrame reason={t_i18n("No case yet: the measures start with the first case the loop opens")} testId="ctem-governance-loop-empty" />;
  }
  const proof = m.closed_on_proof;
  return (
    <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5" data-testid="ctem-governance-loop-measures">
      {LOOP_STEPS.map((s) => (
        <div key={s.key} className="rounded-sm border border-elevation-subtle-soft p-3" data-testid={`ctem-governance-loop-${s.key}`}>
          <dt className="content-caption text-default-secondary">{t_i18n(s.label)}</dt>
          <dd className="content-compact font-medium">{duration(m[s.key].median_hours)}</dd>
          {/* A step no case reached says "No case yet" alone (UX round 1 A2 on #941). */}
          {m[s.key].count > 0 && (
            <dd className="content-caption text-default-secondary">
              {t_i18n("Median of {count, plural, one {# case} other {# cases}}", { count: m[s.key].count })}
            </dd>
          )}
        </div>
      ))}
      <div className="rounded-sm border border-elevation-subtle-soft p-3" data-testid="ctem-governance-loop-closed_on_proof">
        <dt className="content-caption text-default-secondary">{t_i18n("Closed on proof")}</dt>
        <dd className="content-compact font-medium">
          {proof.share == null ? t_i18n("No resolved case yet") : `${Math.round(proof.share * 100)}%`}
        </dd>
        <dd className="content-caption text-default-secondary">
          {t_i18n("{proven} of {count, plural, one {# resolved case} other {# resolved cases}}", { proven: proof.proven, count: proof.resolved })}
        </dd>
      </div>
    </dl>
  );
}

export default function CtemGovernance() {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const { data: user } = useMe();
  const vertical: CtemVertical = user?.ctem_vertical ?? "cyber";
  const { data: posture, isLoading } = useCtemPosture(vertical);
  const { data: histResp } = useCtemPostureHistory(vertical);
  const { data: context } = useOrganizationContext();
  const save = usePatchOrganizationContext();
  const [editing, setEditing] = useState(false);
  const today = new Date(useNow());

  if (isLoading && !posture) return <PageSkeleton cards={3} />;

  const score = posture?.posture_score ?? null;
  const band = posture?.posture_band ?? "unknown";
  const profile = context?.profile;
  const target = profile?.posture_target ?? null;
  const gap = postureGap(score, target);
  const history = historyWindow(histResp?.history ?? [], GOVERNANCE_DAYS, today);
  const days = history.map((h) => ({ label: formatTrendDay(intl, h.date), score: h.posture_score }));
  const recordedDays = days.filter((d) => d.score != null).length;
  const risks = risksByExpiry(profile?.prioritization_policy?.accepted_risks ?? [], today);
  const crq = posture?.products?.opencrq;
  const crqUrl = crq?.present ? safeExternalUrl(crq.url ?? undefined) : undefined;

  const saveTarget = (value: number) => {
    if (!profile) return;
    save.mutate(
      // #1752: the target alone travels, never the asset rows.
      { fields: { posture_target: value }, based_on_version: context?.version ?? null },
      {
        onSuccess: () => {
          setEditing(false);
          toast({ title: t_i18n("Posture target saved"), variant: "success" });
        },
        onError: (err) => toast({ title: t_i18n("The posture target was not saved"), description: err.message, variant: "error" }),
      },
    );
  };

  const postureBand = CTEM_BAND_CONFIG[band] ?? CTEM_BAND_CONFIG.unknown;
  const headline =
    gap == null || target == null ? null : gap > 0
      ? t_i18n("{gap, plural, one {# point} other {# points}} below the target of {target}", { gap, target })
      : t_i18n("At or above the target of {target}", { target });

  return (
    <div className="space-y-6 animate-fade-in" data-testid="ctem-governance-page">
      {/* The one CTEM header band (#11660): the posture and the target as
          cells, the definition behind the "i", the gap on the breadcrumb line. */}
      <CtemPageHeader
        back={{ to: "/", label: "Back to Command Center" }}
        crumbExtras={
          headline ? (
            <Badge variant="outline" className={gap != null && gap > 0 ? "text-feedback-error-primary" : "text-feedback-success-primary"} data-testid="ctem-governance-gap">
              {headline}
            </Badge>
          ) : null
        }
        title={t_i18n("Governance & Risk")}
        hint={t_i18n("The posture against the target the organization set, and the risks it accepted")}
        hintLabel={t_i18n("About {name}", { name: t_i18n("Governance & Risk") })}
        stats={[
          {
            id: "posture",
            label: t_i18n("Exposure posture"),
            value: <span className={postureBand.color}>{score ?? "--"}</span>,
            sub: score != null ? <span className={postureBand.color}>{t_i18n(postureBand.label)}</span> : undefined,
          },
          {
            id: "target",
            label: t_i18n("Posture target"),
            value: target ?? "--",
          },
        ]}
      />

      <HeroCard title={t_i18n("Posture against target")} testId="ctem-governance-hero">
        {target == null || editing ? (
          isAdmin ? (
            editing ? (
              <TargetForm initial={target} onSave={saveTarget} onCancel={() => setEditing(false)} pending={save.isPending} />
            ) : (
              <NotConfiguredFrame
                reason={t_i18n("No posture target yet: set the score the organization aims for")}
                action={t_i18n("Set a posture target")}
                onAction={() => setEditing(true)}
                testId="ctem-governance-no-target"
              />
            )
          ) : (
            <NotConfiguredFrame reason={t_i18n("No posture target yet: an administrator sets it")} testId="ctem-governance-no-target" />
          )
        ) : (
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="content-compact text-default-secondary">{t_i18n("Target {value}", { value: target })}</p>
            {isAdmin && (
              <Button type="button" size="sm" variant="outline" onClick={() => setEditing(true)} data-testid="ctem-governance-edit-target">
                {t_i18n("Change the target")}
              </Button>
            )}
          </div>
        )}
        {recordedDays === 0 ? (
          <NotConfiguredFrame reason={t_i18n("No posture recorded yet: the first day appears after the next sync")} testId="ctem-governance-no-history" />
        ) : (
          <>
            {recordedDays < 2 && (
              <p className="content-caption text-default-secondary">{t_i18n("Trend after 2 days of history")}</p>
            )}
            <PostureVsTarget days={days} target={editing ? null : target} />
          </>
        )}
        <HiddenChartValues
          caption={t_i18n("Posture against target")}
          head={[t_i18n("Day"), t_i18n("Posture"), t_i18n("Target")]}
          rows={days.map((d) => [d.label, d.score == null ? "--" : Math.round(d.score), target ?? "--"])}
        />
      </HeroCard>

      {/* The decisions waiting and the risks accepted on cases (#1621): one full-width row. */}
      <CasesInPhaseCard stage="governance" backTo="/ctem/governance" />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <HeroCard title={t_i18n("Accepted risks")} testId="ctem-governance-risks">
          {risks.length === 0 ? (
            <NotConfiguredFrame
              reason={t_i18n("No accepted risk with an expiry in the prioritization policy")}
              action={isAdmin ? t_i18n("Open the prioritization policy") : undefined}
              onAction={isAdmin ? () => navigate(profileFieldHref("accepted_risks")) : undefined}
              testId="ctem-governance-no-risks"
            />
          ) : (
            <RiskStrip risks={risks} />
          )}
          <HiddenChartValues
            caption={t_i18n("Accepted risks by days until expiry")}
            head={[t_i18n("Subject"), t_i18n("Owner"), t_i18n("Expires")]}
            rows={risks.map((r) => [r.subject, r.owner, r.expires_on])}
          />
        </HeroCard>

        <HeroCard
          title={t_i18n("SLA adherence")}
          chips={<Badge variant="outline" className="content-caption">{t_i18n("Soon")}</Badge>}
          testId="ctem-governance-sla"
        >
          <div className="flex items-start gap-3">
            <img src={CTEM_PRODUCT_INFO.opencrq.logo} alt="" className="mt-0.5 h-5 w-5 shrink-0" />
            <NotConfiguredFrame
              reason={
                crq?.present
                  ? t_i18n("OpenCRQ is connected: SLA adherence arrives with its remediation SLAs")
                  : t_i18n("SLA adherence needs OpenCRQ")
              }
              action={crqUrl ? t_i18n("Open {name}", { name: "OpenCRQ" }) : undefined}
              href={crqUrl}
              testId="ctem-governance-sla-not-configured"
            />
          </div>
        </HeroCard>
      </div>

      <HeroCard
        title={t_i18n("Loop measures")}
        hint={t_i18n("How fast the loop runs, read from the history of the cases you can see")}
        testId="ctem-governance-loop"
      >
        <LoopMeasures />
      </HeroCard>
    </div>
  );
}
