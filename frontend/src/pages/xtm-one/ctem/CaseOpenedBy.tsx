// SPDX-License-Identifier: Apache-2.0

/**
 * "Opened by" on a case page (CTEM-03-UI, #892; dev-docs/ctem-loop-visibility.md
 * sections 3 and 4): the signals that opened or joined the case, each with its
 * lens, its trigger path and the scope gate's reason in one sentence, and a
 * link to its loop trace (`?loop=signal:<id>`). Read through
 * `GET /ctem-signals?case_id=`, which applies the reader's visibility; a case
 * opened by no signal (one the recompute built from source issues) says so.
 */
import { Link } from "react-router-dom";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useCtemCaseSignals } from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";
import { formatDayIntl } from "@/lib/relative-time";
import { decisionSentence } from "./loop/DecisionReason";
import { loopParam } from "./loop/loop-stages";

const LENS_LABEL: Record<string, string> = { inside: "Inside", outside: "Outside", world: "World" };

export function CaseOpenedBy({ caseId }: { caseId: string }) {
  const { t_i18n, intl } = useFormatter();
  const { data, isLoading, isError } = useCtemCaseSignals(caseId);
  const signals = data?.signals ?? [];

  return (
    <Card data-testid="ctem-case-opened-by">
      <CardHeader>
        <CardTitle>{t_i18n("Opened by")}</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading && !data ? (
          <Skeleton className="h-10 w-full" />
        ) : isError && !data ? (
          <p className="content-base text-default-secondary">{t_i18n("The signals of this case could not be read.")}</p>
        ) : signals.length === 0 ? (
          <p className="content-base text-default-secondary">{t_i18n("No signal opened this case: it groups open issues read from the products.")}</p>
        ) : (
          <ul className="space-y-3">
            {signals.map((s) => (
              <li key={s.id} className="flex min-w-0 flex-col gap-0.5 content-base" data-testid="ctem-case-opened-by-signal">
                <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
                  <span className="font-medium">{t_i18n(s.trigger_path_label || s.trigger_path)}</span>
                  <span className="text-default-secondary">{t_i18n(LENS_LABEL[s.lens] ?? s.lens)}</span>
                  <span className="min-w-0 truncate text-default-secondary" title={s.subject.label || s.subject.ref}>
                    {s.subject.label || s.subject.ref}
                  </span>
                  <Link
                    to={`?loop=${encodeURIComponent(loopParam("signal", s.id))}`}
                    className="ml-auto whitespace-nowrap text-accent underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                  >
                    {t_i18n("See the loop trace")}
                  </Link>
                </span>
                <span className="text-default-secondary">
                  {decisionSentence(s.decision, t_i18n, (iso) => formatDayIntl(intl, iso))}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
