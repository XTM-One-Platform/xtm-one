// SPDX-License-Identifier: Apache-2.0

/**
 * "Attack path and blast radius" on a case page (CTEM-23-UI, #1289): from the
 * affected asset, the hops an attacker takes along the OpenAEV attack paths,
 * nearest first, each one proven by an execution or inferred, and the primary
 * (business) assets behind the assets reached. Written by the triage step from
 * OpenCRQ; a viewer who does not read OpenCRQ sees the shape with "Restricted
 * asset". Never blank: a case with no reading says why.
 */
import { Chip } from "@filigran/design-system";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { CtemCaseAttackPath } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { CaseSectionReason } from "./CaseSectionReason";

/** The exposure words, translated where drawn. @i18n-keys */
const EXPOSURE_LABEL = {
  internet_facing: "Internet-facing",
  internal: "Internal",
  unknown: "Exposure unknown",
} as const;

/** How a hop is known, translated where drawn. @i18n-keys */
const NATURE_LABEL = {
  proven: "Proven",
  inferred: "Inferred",
} as const;

const RESTRICTED = "Restricted";

export function CaseAttackPath({ reading }: { reading: CtemCaseAttackPath | null | undefined }) {
  const { t_i18n } = useFormatter();
  const name = (asset: string) => (asset === RESTRICTED ? t_i18n("Restricted asset") : asset);

  return (
    <Card data-testid="ctem-case-attack-path">
      <CardHeader>
        <CardTitle>{t_i18n("Attack path and blast radius")}</CardTitle>
      </CardHeader>
      <CardContent>
        {!reading ? (
          <div className="content-base">
            <CaseSectionReason
              own={t_i18n("No attack path yet: triage reads it from OpenCRQ once an OpenAEV simulation has mapped the affected asset.")}
              needsOpencrq
              testId="ctem-case-attack-path-empty"
            />
          </div>
        ) : (
          <div className="flex min-w-0 flex-col gap-3 content-base">
            <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
              <span className="text-default-secondary">{t_i18n("From")}</span>
              <span className="min-w-0 truncate font-medium" title={name(reading.origin.asset)}>
                {name(reading.origin.asset)}
              </span>
              <span className="text-default-secondary">{t_i18n(EXPOSURE_LABEL[reading.origin.exposure] ?? EXPOSURE_LABEL.unknown)}</span>
            </span>
            {reading.hops.length === 0 ? (
              <span className="text-default-secondary" data-testid="ctem-case-attack-path-no-hop">
                {t_i18n("No attack path leaves this asset in the latest simulations.")}
              </span>
            ) : (
              <ol className="flex flex-col gap-1" aria-label={t_i18n("Hops, nearest first")}>
                {reading.hops.map((h, i) => (
                  <li key={`${h.from}-${h.to}-${i}`} className="flex min-w-0 flex-wrap items-center gap-x-2" data-testid="ctem-case-attack-path-hop">
                    <span className="min-w-0 truncate" title={name(h.from)}>
                      {name(h.from)}
                    </span>
                    <span aria-hidden="true" className="text-default-secondary">
                      →
                    </span>
                    <span className="min-w-0 truncate font-medium" title={name(h.to)}>
                      {name(h.to)}
                    </span>
                    <Chip label={t_i18n(NATURE_LABEL[h.nature] ?? NATURE_LABEL.inferred)} className="shrink-0" />
                    {h.simulation && (
                      <span className="text-default-secondary">{t_i18n("Simulation {id}", { id: h.simulation })}</span>
                    )}
                  </li>
                ))}
              </ol>
            )}
            <div className="flex min-w-0 flex-col gap-1">
              <span className="text-default-secondary">{t_i18n("Primary assets reached")}</span>
              {reading.primary.length === 0 ? (
                <span data-testid="ctem-case-attack-path-no-primary">{t_i18n("None: no business asset sits behind the assets reached.")}</span>
              ) : (
                <ul className="flex flex-wrap items-center gap-1">
                  {reading.primary.map((p, i) => (
                    <li key={`${p}-${i}`} data-testid="ctem-case-attack-path-primary">
                      <Chip label={name(p)} className="shrink-0" />
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
