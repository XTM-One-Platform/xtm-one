// SPDX-License-Identifier: Apache-2.0

/**
 * The signals of the Discovery stage by trigger path and lens (CTEM-30, #937;
 * dev-docs/ctem-loop-visibility.md sections 2 and 6): the 12 paths of the
 * Trigger Model, grouped by the lens that usually sees them, each with the
 * signals the reader can open, by state. The status of a path is the one the
 * Signal sources dialog shows (pathStatus, UX round 1 B1 on #1194): a path
 * that is not connected reads so, with the same Connect target as the dialog,
 * never 0: a gap never reads as safe. The signals themselves open from the command
 * center's signals counter (the loop trace list); this card says where they
 * come from.
 *
 * Read through `GET /ctem-signals/paths`, the same visibility as the signal
 * list; the `ctem_signal_update` stream frame refreshes it.
 */
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { useCtemSignalPaths, type CtemSignalPath, type CtemSignalState } from "@/hooks/api/useCtemSignalsApi";
import { useAuth } from "@/hooks/useAuth";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { HeroCard } from "../stage-heroes";
import { SIGNAL_STATE_LABEL } from "./DecisionReason";
import { connectHref, connectNeedsAdmin, connectTarget, isLiveStatus, PATH_STATUS_LABEL, pathStatus } from "./signal-sources";

const LENSES = ["inside", "outside", "world"] as const;

/** The lens headings. @i18n-keys */
const LENS_HEADING = { inside: "Inside", outside: "Outside", world: "World" } as const;

/** The states a path row names, in loop order. */
const ROW_STATES: readonly CtemSignalState[] = ["new", "to_validate", "watchlist", "case_opened"];

/**
 * One path (UX round 1 B1 on #941): the name on line 1 at full width, never
 * cut; the counts of a connected path, or its status with the dialog's Connect
 * action, on line 2 in one row that does not wrap.
 */
function PathRow({ path }: { path: CtemSignalPath }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { count, exactCount } = useNumberFormat();
  const { isAdmin } = useAuth();
  const status = pathStatus(path);
  const live = isLiveStatus(status);
  const unbound = live ? null : connectTarget(path);
  // A profile pass is turned on by an administrator: a reader gets no button for it.
  const target = unbound && (isAdmin || !connectNeedsAdmin(unbound)) ? unbound : null;
  return (
    <li className="flex min-w-0 flex-col gap-1 py-1.5 content-compact" data-testid="ctem-trigger-path" data-path={path.id} data-status={status}>
      <span className="min-w-0 font-medium" data-testid="ctem-trigger-path-name">{t_i18n(path.label)}</span>
      {live ? (
        <span className="flex flex-nowrap items-center gap-1 whitespace-nowrap content-caption">
          {ROW_STATES.map((state) => {
            const n = path.counts[state] ?? 0;
            const label = t_i18n(SIGNAL_STATE_LABEL[state]);
            return (
              <span key={state} data-state={state} className={cn("inline-flex items-center gap-1 px-1.5 py-0.5", n === 0 && "text-default-secondary")}>
                <span className="tabular-nums font-medium" title={exactCount(n)}>{count(n)}</span>
                <span>{label}</span>
              </span>
            );
          })}
        </span>
      ) : (
        <span className="flex flex-nowrap items-center gap-2 whitespace-nowrap content-caption">
          <span className="text-default-secondary" data-testid="ctem-trigger-path-status">{t_i18n(PATH_STATUS_LABEL[status])}</span>
          {target && (
            <Button variant="ghost" size="sm" onClick={() => navigate(connectHref(target))} data-testid="ctem-trigger-path-connect">{target.off ? t_i18n("Turn on") : t_i18n("Connect")}</Button>
          )}
        </span>
      )}
    </li>
  );
}

export function TriggerPathsCard() {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useCtemSignalPaths();
  const paths = data?.paths ?? [];
  // No connected-count caption nor sources button (Sam, 23 Nov 2026, #1344): each row carries its status.
  return (
    <HeroCard title={t_i18n("Signals by trigger path")} testId="ctem-trigger-paths">
      {isLoading ? (
        <p className="content-compact text-default-secondary">{t_i18n("Loading...")}</p>
      ) : (
        // Three lens columns only where a count row fits on one line (2xl); one column below.
        <div className="grid gap-4 2xl:grid-cols-3">
          {LENSES.map((lens) => (
            <div key={lens} className="min-w-0" data-testid={`ctem-trigger-lens-${lens}`}>
              <h3 className="content-caption font-medium text-default-secondary">{t_i18n(LENS_HEADING[lens])}</h3>
              <ul className="divide-y divide-[var(--border-elevation-subtle)]">
                {paths.filter((p) => p.default_lens === lens).map((p) => (
                  <PathRow key={p.id} path={p} />
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </HeroCard>
  );
}
