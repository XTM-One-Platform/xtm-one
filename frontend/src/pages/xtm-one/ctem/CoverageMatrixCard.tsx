// SPDX-License-Identifier: Apache-2.0

/**
 * Coverage widget of the CTEM command center (#9050 slice 4): the five CTEM
 * stages against the three XTM Platform modules, read from the posture
 * (`lib/ctem-coverage-stats.ts`). A cell says whether a connected module feeds
 * the stage; a primary source not connected is a blind spot with its connect
 * path, one click away. Deterministic: no model, no AI gate, no sample figure.
 */
import { Check, Grid3x3, Minus, Plus } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { CTEM_PRODUCT_INFO } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { COVERAGE_MODULES, type CoverageCell, type CoverageMatrix, type CoverageModule } from "@/lib/ctem-coverage-stats";
import { stageName } from "./stats-coverage";

/** What a cell means, said in its tooltip and to a screen reader. @i18n-keys */
const CELL_SENTENCES = {
  live: "{module} feeds this stage and is connected",
  missing: "{module} is a main source of this stage and is not connected",
  optional: "{module} adds to this stage and is not connected",
  none: "{module} does not feed this stage",
} as const;

function Cell({ cell, module }: { cell: CoverageCell; module: string }) {
  const { t_i18n } = useFormatter();
  const sentence = t_i18n(CELL_SENTENCES[cell], { module });
  return (
    <Tip content={sentence}>
      <span
        tabIndex={0}
        className={cn(
          "inline-flex h-6 w-6 items-center justify-center rounded-sm",
          cell === "live" && "bg-alert-success text-feedback-success-primary",
          cell === "missing" && "ring-1 ring-feedback-warning-primary text-feedback-warning-primary",
          // A ring in the secondary text ink keeps 3:1 or more in both themes (#9237 A1, A2).
          cell === "optional" && "ring-1 ring-current text-default-secondary",
          cell === "none" && "text-default-disabled",
          TIP_WRAP_FOCUS_RING,
        )}
      >
        {cell === "live" ? (
          <Check className="h-3.5 w-3.5" aria-hidden="true" />
        ) : cell === "none" ? (
          <Minus className="h-3 w-3" aria-hidden="true" />
        ) : (
          <Plus className="h-3 w-3" aria-hidden="true" />
        )}
        <span className="sr-only">{sentence}</span>
      </span>
    </Tip>
  );
}

export function CoverageMatrixCard({
  matrix,
  onConnect,
}: {
  matrix: CoverageMatrix | null;
  onConnect: (module: CoverageModule) => void;
}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const name = (m: string) => CTEM_PRODUCT_INFO[m]?.name ?? m;

  return (
    <Card className="@container h-full overflow-hidden flex flex-col">
      <CardContent className="p-4 flex flex-col flex-1 min-h-0 gap-3">
        <div className="flex min-h-7 items-center justify-between gap-2">
          <h2 className="content-base font-semibold flex items-center gap-2">
            <Grid3x3 className="h-4 w-4 text-accent" /> {t_i18n("Coverage")}
          </h2>
          {matrix && (
            <span className="content-caption text-default-secondary tabular-nums">
              {t_i18n("{covered} of {total} stages with data", { covered: matrix.covered, total: matrix.rows.length })}
            </span>
          )}
        </div>
        {!matrix ? (
          <EmptyState
            variant="inset"
            icon={Grid3x3}
            title={t_i18n("Coverage not read yet")}
            description={t_i18n("The coverage shows once the exposure posture is read from the connected platforms.")}
          />
        ) : (
          <div className="min-h-0 flex-1 overflow-auto">
            <table className="w-full content-compact">
              <caption className="sr-only">{t_i18n("CTEM stages against the XTM Platform modules")}</caption>
              <thead>
                <tr className="text-left content-caption text-default-secondary">
                  <th scope="col" className="font-medium pb-2">{t_i18n("Stage")}</th>
                  {COVERAGE_MODULES.map((m) => (
                    <th key={m} scope="col" className="font-medium pb-2 text-center">{name(m)}</th>
                  ))}
                  <th scope="col" className="font-medium pb-2 text-right">{t_i18n("Score")}</th>
                </tr>
              </thead>
              <tbody>
                {matrix.rows.map((row) => (
                  <tr key={row.key} className="border-t border-elevation-subtle-soft">
                    <th scope="row" className="py-1.5 text-left font-normal">
                      <button
                        type="button"
                        className={cn("text-left hover:underline rounded-sm", TIP_WRAP_FOCUS_RING)}
                        onClick={() => navigate(`/ctem/${row.key}`)}
                      >
                        {/* The stage name by key, localized (#9237 A4), never the server label. */}
                        {stageName(row.key, row.label, t_i18n)}
                      </button>
                    </th>
                    {COVERAGE_MODULES.map((m) => (
                      <td key={m} className="py-1.5 text-center">
                        <Cell cell={row.cells[m]} module={name(m)} />
                      </td>
                    ))}
                    <td className="py-1.5 text-right tabular-nums">
                      {row.score === null ? (
                        <Tip content={t_i18n("Not measured yet")}>
                          <span tabIndex={0} className={cn("text-default-disabled", TIP_WRAP_FOCUS_RING)}>
                            <span aria-hidden="true">-</span>
                            <span className="sr-only">{t_i18n("Not measured yet")}</span>
                          </span>
                        </Tip>
                      ) : (
                        row.score
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {matrix && (
          <div className="flex flex-wrap items-center gap-2">
            {COVERAGE_MODULES.filter((m) => matrix.rows.some((r) => r.missing.includes(m))).map((m) => (
              <Button key={m} variant="secondary" size="sm" className="h-7 content-compact gap-1" onClick={() => onConnect(m)}>
                <Plus className="h-3 w-3" />
                {t_i18n("Connect {name} for {count, plural, one {# stage} other {# stages}}", {
                  name: name(m),
                  count: matrix.rows.filter((r) => r.missing.includes(m)).length,
                })}
              </Button>
            ))}
            {!matrix.feedsAnyStage.opencrq && (
              <p className="content-caption text-default-secondary">
                {t_i18n("OpenCRQ does not feed a CTEM stage yet.")}
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
