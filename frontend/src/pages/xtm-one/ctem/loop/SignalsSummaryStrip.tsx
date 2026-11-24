// SPDX-License-Identifier: Apache-2.0

/**
 * The summary strip of the Signals page (#1263, Sam, 22 Nov 2026): four cards
 * above the signals list, so the page reads at a glance and an empty list
 * still carries the sources' context.
 *
 * - Signals in 30 days: the paths' `count_30d`, the all-time total under it.
 * - Unmatched: the signals no inventory entry holds, parked until one does (#1505), the new ones under it.
 * - Signals with a case: the signals in state case_opened (many feed one case), the
 *   open cases under it, read from the Cases page's own query and open filter so the
 *   two pages never disagree (#1935: the card said "Cases opened" over a signal count).
 * - Trigger paths connected: N of the live catalogue, the connected sources'
 *   logos and the Sources link on its subtitle line.
 *
 * One row of four footerless cards from 1024 px up, every card the height of a
 * plain StatCard (UX round 4 on #1272: a 2x2 strip at 1080 pushed the table out
 * of its fit-to-viewport mode and the page scrolled; only the fourth card had a
 * footer, so the other three carried a void under their subtitle).
 *
 * The state counts are the key row's (`useRecentCtemSignals`, the same read the
 * command center makes), so the page and the key row never disagree. The first
 * three cards open the signals list on their state; the fourth carries the logos
 * and a link to the signal sources page (#1519).
 */
import { type ComponentProps } from "react";
import { Link as RouterLink, useNavigate } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { StatCard } from "@/components/ui/stat-card";
import { Tip } from "@/components/ui/tooltip";
import { isOpenCaseState, useCtemCases } from "@/hooks/api/useCtemCasesApi";
import { CTEM_SIGNAL_STATES, useCtemSignalPaths, useRecentCtemSignals } from "@/hooks/api/useCtemSignalsApi";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { CTEM_PRODUCT_INFO, INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { cn } from "@/lib/utils";
import { connectedCount } from "./signal-sources";
import { SIGNAL_SOURCES_PATH, signalsHref } from "./signals-route";

/* Library glyphs handed to StatCard, which takes a glyph component (no direct lucide-react import). */
type Glyph = ComponentProps<typeof StatCard>["icon"];
const SignalsGlyph = (({ className }: { className?: string }) => <Icon name="activity" size={16} className={className} />) as unknown as Glyph;
const UnmatchedGlyph = (({ className }: { className?: string }) => <Icon name="minus" size={16} className={className} />) as unknown as Glyph;
const CaseGlyph = (({ className }: { className?: string }) => <Icon name="briefcase" size={16} className={className} />) as unknown as Glyph;
const SourcesGlyph = (({ className }: { className?: string }) => <Icon name="satellite-dish" size={16} className={className} />) as unknown as Glyph;

/** How many connected sources the fourth card shows by logo on its subtitle line; the rest is "+N". */
const LOGOS_SHOWN = 3;

function providerName(provider: string): string {
  return CTEM_PRODUCT_INFO[provider]?.name ?? INTEGRATION_PROVIDER_NAMES[provider] ?? provider;
}

export function SignalsSummaryStrip() {
  const { t_i18n } = useFormatter();
  const { count, exactCount } = useNumberFormat();
  const navigate = useNavigate();
  const { data: recent } = useRecentCtemSignals();
  const { data: pathData } = useCtemSignalPaths();
  const paths = pathData?.paths ?? [];
  const counts = recent?.counts;
  // A section of the page: a refused cases read leaves the subtitle pending, never the page to the error boundary.
  const { data: casesData } = useCtemCases({ throwOnError: false });
  const caseRows = casesData?.cases;
  const openCases = Array.isArray(caseRows) ? caseRows.filter((c) => isOpenCaseState(c.state)).length : undefined;
  const total = counts ? CTEM_SIGNAL_STATES.reduce((sum, s) => sum + (counts[s] ?? 0), 0) : undefined;
  const last30 = pathData ? paths.reduce((sum, p) => sum + (p.count_30d ?? 0), 0) : undefined;
  const connected = connectedCount(paths);
  // The connected sources, one logo per provider, the providers with the most signals on their paths first.
  const weight = new Map<string, number>();
  for (const p of paths) {
    for (const s of p.sources ?? []) {
      if (s.bound) weight.set(s.provider, (weight.get(s.provider) ?? 0) + (p.total ?? 0));
    }
  }
  const providers = [...weight.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([provider]) => provider);
  const shown = providers.slice(0, LOGOS_SHOWN);
  const pending = "-";

  return (
    <div className="grid grid-cols-2 gap-4 lg:grid-cols-4" data-testid="ctem-signals-summary">
      <StatCard
        icon={SignalsGlyph}
        label={t_i18n("Signals in 30 days")}
        value={last30 ?? pending}
        subtitle={total === undefined ? pending : t_i18n("{count, plural, one {# signal in all} other {# signals in all}}", { count: total })}
        onClick={() => navigate(signalsHref())}
      />
      <StatCard
        icon={UnmatchedGlyph}
        label={t_i18n("Unmatched")}
        value={counts ? (counts.to_validate ?? 0) : pending}
        subtitle={counts ? t_i18n("{count, plural, one {# new} other {# new}}", { count: counts.new ?? 0 }) : pending}
        onClick={() => navigate(signalsHref({ state: "to_validate" }))}
      />
      <StatCard
        icon={CaseGlyph}
        label={t_i18n("Signals with a case")}
        value={counts ? (counts.case_opened ?? 0) : pending}
        subtitle={openCases === undefined ? pending : t_i18n("{count, plural, =0 {No open case} one {# open case} other {# open cases}}", { count: openCases })}
        onClick={() => navigate(signalsHref({ state: "case_opened" }))}
      />
      <StatCard
        icon={SourcesGlyph}
        label={t_i18n("Trigger paths connected")}
        value={pathData ? `${count(connected)}/${count(paths.length)}` : pending}
        exact={pathData ? t_i18n("{connected} of {total} trigger paths connected", { connected: exactCount(connected), total: exactCount(paths.length) }) : undefined}
        subtitle={
          /* Spans only: StatCard renders the subtitle inside a <p>. One caption line, so the
             card keeps the height of the three cards beside it (UX round 4 B2 on #1272). */
          <span className="flex min-w-0 items-center gap-2" data-testid="ctem-signals-summary-sources">
            {shown.length > 0 ? (
              <span
                role="list"
                className="flex min-w-0 items-center gap-1"
                aria-label={t_i18n("{count, plural, one {# source connected} other {# sources connected}}", { count: providers.length })}
              >
                {shown.map((provider) => (
                  <span role="listitem" key={provider} data-provider={provider} className="inline-flex">
                    <Tip content={providerName(provider)}>
                      <span className={cn("inline-flex size-4 items-center justify-center rounded-sm", BUTTON_ROLE_FOCUS_RING)} tabIndex={0}>
                        <ProviderLogo provider={provider} className="size-4" />
                      </span>
                    </Tip>
                  </span>
                ))}
                {providers.length > shown.length && (
                  <span role="listitem" className="tabular-nums">+{count(providers.length - shown.length)}</span>
                )}
              </span>
            ) : (
              <span className="min-w-0 truncate">{t_i18n("No source connected yet")}</span>
            )}
            <RouterLink
              to={SIGNAL_SOURCES_PATH}
              className={cn("ml-auto shrink-0 rounded-sm text-accent hover:underline", BUTTON_ROLE_FOCUS_RING)}
              data-testid="ctem-signals-summary-sources-link"
            >
              {t_i18n("Sources")}
            </RouterLink>
          </span>
        }
      />
    </div>
  );
}
