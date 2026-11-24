// SPDX-License-Identifier: Apache-2.0

/**
 * "Attack path and blast radius" on a case page (CTEM-23-UI, #1289): from the
 * affected asset, the hops an attacker takes along the OpenAEV attack paths,
 * nearest first, each one proven by an execution or inferred, and the primary
 * (business) assets behind the assets reached. Written by the triage step from
 * OpenCRQ; a viewer who does not read OpenCRQ sees the shape with "Restricted
 * asset". Never blank: a case with no reading says why.
 */
import { Chip, Icon } from "@filigran/design-system";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CASE_SECTION_ICON, CaseCardTitleText } from "./case-icons";
import type { CtemCaseAttackPath } from "@/hooks/api/useCtemCasesApi";
import { useFormatter } from "@/i18n";
import { safeExternalUrl } from "@/lib/utils";
import { CaseSectionReason } from "./CaseSectionReason";
import { CaseFoldToggle, useCaseFold } from "./case-fold";

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

/**
 * The colour of each hop (UX round 1 on #1296, B1): a hop an execution proved
 * reads high, an inferred one reads info, as RETEST_TONE does in case-loop.tsx.
 */
const NATURE_TONE: Record<keyof typeof NATURE_LABEL, "high" | "info"> = {
  proven: "high",
  inferred: "info",
};

type Hop = CtemCaseAttackPath["hops"][number];

/** What proved a hop, in words (B2): the simulation's name, else the date, never its id. */
function ProvenBy({ hop }: { hop: Hop }) {
  const { t_i18n, intl } = useFormatter();
  if (hop.nature !== "proven" || (!hop.simulation_name && !hop.seen_at)) return null;
  const date = hop.seen_at ? intl.formatDate(hop.seen_at, { dateStyle: "medium" }) : "";
  const text = hop.simulation_name
    ? date
      ? t_i18n("Proven by {name}, {date}", { name: hop.simulation_name, date })
      : t_i18n("Proven by {name}", { name: hop.simulation_name })
    : t_i18n("Proven by an OpenAEV simulation, {date}", { date });
  const href = safeExternalUrl(hop.simulation_url ?? undefined);
  if (!href) {
    return (
      <span className="min-w-0 truncate text-default-secondary" data-testid="ctem-case-attack-path-proof">
        {text}
      </span>
    );
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex min-w-0 items-center gap-1 text-default-secondary hover:underline"
      data-testid="ctem-case-attack-path-proof"
    >
      <span className="truncate">{text}</span>
      <Icon name="external-link" size={16} className="shrink-0" />
      <span className="sr-only">{t_i18n("Open in OpenAEV")}</span>
    </a>
  );
}

const RESTRICTED = "Restricted";

export function CaseAttackPath({ reading }: { reading: CtemCaseAttackPath | null | undefined }) {
  const { t_i18n } = useFormatter();
  const name = (asset: string) => (asset === RESTRICTED ? t_i18n("Restricted asset") : asset);
  // Up to 40 hops per reading (ATTACK_PATH_MAX in ctem_case_loop): folded past five, nearest kept (#1679, point 5).
  const hops = useCaseFold(reading?.hops ?? []);

  return (
    <Card data-testid="ctem-case-attack-path">
      <CardHeader>
        <CardTitle>
          <CaseCardTitleText icon={CASE_SECTION_ICON.attack_path}>{t_i18n("Attack path and blast radius")}</CaseCardTitleText>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {!reading ? (
          <div className="content-base">
            <CaseSectionReason
              own={t_i18n("No attack path yet: triage reads it from OpenCRQ once an OpenAEV simulation has mapped the affected asset.")}
              source="opencrq"
              testId="ctem-case-attack-path-empty"
            />
          </div>
        ) : (
          <div className="flex min-w-0 flex-col gap-3 content-base">
            <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
              <span className="text-default-secondary">{t_i18n("Attack starts from")}</span>
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
              <div className="flex flex-col gap-1">
                <ol className="flex flex-col gap-1" aria-label={t_i18n("Hops, nearest first")} id="ctem-case-attack-path-hops">
                  {hops.visible.map((h, i) => (
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
                      <Chip
                        label={t_i18n(NATURE_LABEL[h.nature] ?? NATURE_LABEL.inferred)}
                        severity={NATURE_TONE[h.nature] ?? NATURE_TONE.inferred}
                        className="shrink-0"
                        data-testid="ctem-case-attack-path-nature"
                      />
                      <ProvenBy hop={h} />
                    </li>
                  ))}
                </ol>
                <CaseFoldToggle fold={hops} controls="ctem-case-attack-path-hops" testId="ctem-case-attack-path-more" />
              </div>
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
