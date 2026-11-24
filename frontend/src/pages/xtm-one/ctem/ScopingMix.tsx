// SPDX-License-Identifier: Apache-2.0
/**
 * The mixed views of the Scoping stage page (#11998, design round 5 approved):
 * the intelligence scope OpenCTI already holds (observables by type, vulnerabilities
 * by CVSS severity), drawn beside the OpenAEV attack surface ring, and one gap filler
 * card per missing module, so no module combination leaves an empty page.
 *
 * Every figure comes from the stage's viz payload (backend `_stage_viz`); a list
 * OpenCTI did not answer is not drawn.
 */
import type { ReactElement } from "react";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { CtemStageViz } from "@/types";
import { HeroCard, HiddenChartValues } from "./stage-heroes";
import { RankedBars, StageGapFiller, type CtemModule } from "./DiscoveryMix";
import { SeverityBars, severityLabelKey } from "./ctem-viz";

/** The readable name of an OpenCTI observable type, read by `t_i18n(OBSERVABLE_TYPE_LABELS[type])`. @i18n-keys */
const OBSERVABLE_TYPE_LABELS = {
  "artifact": "Artifact (observable type)",
  "autonomous-system": "Autonomous system (observable type)",
  "bank-account": "Bank account (observable type)",
  "credential": "Credential (observable type)",
  "cryptocurrency-wallet": "Cryptocurrency wallet (observable type)",
  "cryptographic-key": "Cryptographic key (observable type)",
  "directory": "Directory (observable type)",
  "domain-name": "Domain name (observable type)",
  "email-addr": "Email address (observable type)",
  "email-message": "Email message (observable type)",
  "hostname": "Hostname (observable type)",
  "ipv4-addr": "IPv4 address (observable type)",
  "ipv6-addr": "IPv6 address (observable type)",
  "mac-addr": "MAC address (observable type)",
  "media-content": "Media content (observable type)",
  "mutex": "Mutex (observable type)",
  "network-traffic": "Network traffic (observable type)",
  "payment-card": "Payment card (observable type)",
  "phone-number": "Phone number (observable type)",
  "process": "Process (observable type)",
  "software": "Software (observable type)",
  "stixfile": "File (observable type)",
  "text": "Text (observable type)",
  "url": "URL (observable type)",
  "user-account": "User account (observable type)",
  "user-agent": "User agent (observable type)",
  "windows-registry-key": "Windows registry key (observable type)",
} as const satisfies Record<string, string>;

/** An observable type as the page names it: its catalog key, else the data's name with spaces (an unlisted custom type). */
function observableTypeLabel(name: string, t: (key: string) => string): string {
  const key = name.toLowerCase();
  if (key in OBSERVABLE_TYPE_LABELS) return t(OBSERVABLE_TYPE_LABELS[key as keyof typeof OBSERVABLE_TYPE_LABELS]);
  return name.replace(/[-_]/g, " ").trim() || name;
}

/** The Scoping cards under the hero, then a gap filler per missing module. */
export function ScopingMix({
  viz,
  connected,
  onConnect,
}: {
  viz: CtemStageViz | undefined;
  connected: { opencti: boolean; openaev: boolean; opencrq: boolean };
  onConnect: () => void;
}) {
  const { t_i18n } = useFormatter();
  const observables = (viz?.observables_by_type ?? []).map((r) => ({ label: observableTypeLabel(r.name, t_i18n), value: r.value }));
  const severity = (viz?.vulnerabilities_by_severity ?? []).filter((r) => r.value > 0);
  const dataCards: ReactElement[] = [];
  const gapCards: ReactElement[] = [];

  if (connected.opencti && observables.length > 0) {
    dataCards.push(
      <HeroCard key="observables" title={t_i18n("Observables in scope by type")} testId="ctem-scoping-observables">
        <RankedBars rows={observables} testId="ctem-scoping-observables-bars" />
        <p className="content-compact text-default-secondary">{t_i18n("The most frequent observable types in OpenCTI")}</p>
        <HiddenChartValues
          caption={t_i18n("Observables in scope by type")}
          head={[t_i18n("Type"), t_i18n("Observables")]}
          rows={observables.map((r) => [r.label, r.value])}
        />
      </HeroCard>,
    );
  }
  if (connected.opencti && severity.length > 0) {
    dataCards.push(
      <HeroCard key="severity" title={t_i18n("Vulnerabilities tracked by severity")} testId="ctem-scoping-severity">
        <SeverityBars buckets={severity} testId="ctem-scoping-severity-bars" />
        <p className="content-compact text-default-secondary">{t_i18n("CVSS base severity of the vulnerabilities in OpenCTI")}</p>
        <HiddenChartValues
          caption={t_i18n("Vulnerabilities tracked by severity")}
          head={[t_i18n("Severity"), t_i18n("Vulnerabilities")]}
          rows={severity.map((r) => [t_i18n(severityLabelKey(r.label)), r.value])}
        />
      </HeroCard>,
    );
  }

  const copy: Record<CtemModule, { why: string; adds: string[] }> = {
    OpenCTI: {
      why: t_i18n("OpenCTI says what your intelligence scope holds."),
      adds: [t_i18n("Observables in scope by type"), t_i18n("Vulnerabilities tracked by severity, and scope completeness")],
    },
    OpenAEV: {
      why: t_i18n("OpenAEV says which part of your attack surface you can test."),
      adds: [t_i18n("Attack domains with validation content"), t_i18n("Endpoints, asset groups and security platforms in scope, and the stage score")],
    },
    OpenCRQ: {
      why: t_i18n("OpenCRQ says what the assets in scope are worth at risk."),
      adds: [t_i18n("Expected loss in EUR per business scope"), t_i18n("Share of the total expected loss per scope")],
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
        testId={`ctem-scoping-gap-${module.toLowerCase()}`}
        onConnect={onConnect}
      />,
    );
  }

  if (dataCards.length === 0 && gapCards.length === 0) return null;
  // Same layout rules as Discovery: two columns from 2xl only, and every card of a
  // row ends on the tallest card's edge (Sam, 24 Nov 2026, #1612: equal heights as
  // in the rest of the app; content top-aligned, the free space at the bottom).
  return (
    <div className="space-y-5" data-testid="ctem-scoping-mix">
      {dataCards.length > 0 && (
        <div
          className={cn("grid gap-5 [&>section]:h-full", dataCards.length > 1 && "2xl:grid-cols-2")}
          data-testid="ctem-scoping-data-row"
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
          data-testid="ctem-scoping-gap-row"
          data-columns={Math.min(gapCards.length, 3)}
        >
          {gapCards}
        </div>
      )}
    </div>
  );
}
