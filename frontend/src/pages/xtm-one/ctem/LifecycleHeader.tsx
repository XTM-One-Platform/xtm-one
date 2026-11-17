// SPDX-License-Identifier: Apache-2.0

import { Fragment } from "react";
import { Download, ShieldCheck } from "lucide-react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { CTEM_STAGE_CONFIG, CTEM_STAGE_KEYS } from "@/lib/constants";

/**
 * The title line of the CTEM stage cards (the `lifecycle_header` widget of
 * the command center), with the posture's Export CSV button (D3-5, #9477).
 *
 * Widths (UX round 2 on #9478, B1): at 1024 px the widget's container is
 * about 780 px. The section title is longer in fr, es and ja than in en and
 * the button's label is too, so the stage path subtitle is shown only from
 * the `@4xl` container (896 px): below it the title keeps its full width in
 * every language and never truncates.
 */
export function LifecycleHeader({
  live,
  covered,
  total,
  onExport,
}: {
  live: boolean;
  covered: number;
  total: number;
  onExport: () => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="@container h-full flex items-center">
      <div className="flex w-full min-w-0 items-center justify-between gap-3">
        <h2 className="content-base font-semibold flex items-center gap-2 min-w-0">
          <ShieldCheck className="h-4 w-4 shrink-0 text-accent" /> <span className="truncate">{t_i18n("Exposure lifecycle")}</span>
          {/* The stage path as names joined by chevrons, never ASCII arrows
              (#11689); shown from @6xl, where the five names fit uncut in
              every language, so it never ends in an ellipsis. */}
          <span data-testid="lifecycle-stage-path" className="hidden @6xl:inline-flex shrink-0 items-center gap-1 content-caption font-normal text-default-secondary">
            {CTEM_STAGE_KEYS.map((key, i) => (
              <Fragment key={key}>
                {i > 0 && <Icon name="chevron-right" size={16} aria-hidden className="shrink-0" />}
                <span className="whitespace-nowrap">{t_i18n(CTEM_STAGE_CONFIG[key].label)}</span>
              </Fragment>
            ))}
          </span>
        </h2>
        <div className="flex shrink-0 items-center gap-3">
          <span className="content-compact text-default-secondary">
            {live ? t_i18n("{covered} of {total} stages covered", { covered, total }) : t_i18n("preview")}
          </span>
          {/* The posture as CSV, from the payload already loaded, no model.
              Outline, never filled; off while the cards show the sample.
              wrap="auto": a disabled button never opens its tooltip, the
              wrapper keeps the reason reachable by hover and by Tab. */}
          <Tip
            wrap="auto"
            content={
              live
                ? t_i18n("Download the exposure posture, its stages and KPIs as a CSV file")
                : t_i18n("Available once the posture is computed from a connected platform")
            }
          >
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 gap-1.5"
              onClick={onExport}
              disabled={!live}
              aria-label={t_i18n("Export CSV")}
            >
              <Download className="h-3.5 w-3.5" />
              <span className="hidden @md:inline">{t_i18n("Export CSV")}</span>
            </Button>
          </Tip>
        </div>
      </div>
    </div>
  );
}
