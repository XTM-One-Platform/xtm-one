// SPDX-License-Identifier: Apache-2.0

/**
 * The signals of the Discovery stage by trigger path and lens (CTEM-30, #937;
 * dev-docs/ctem-loop-visibility.md sections 2 and 6): the 12 paths of the
 * Trigger Model, grouped by the lens that usually sees them, each with the
 * signals the reader can open, by state. A path no listener feeds and no
 * signal reached reads "Not configured", never 0, with its connect action: a
 * gap never reads as safe. The signals themselves open from the command
 * center's signals counter (the loop trace list); this card says where they
 * come from.
 *
 * Read through `GET /ctem-signals/paths`, the same visibility as the signal
 * list; the `ctem_signal_update` stream frame refreshes it.
 */
import { Button } from "@/components/ui/button";
import { useCtemSignalPaths, type CtemSignalPath, type CtemSignalState } from "@/hooks/api/useCtemSignalsApi";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { HeroCard } from "../stage-heroes";
import { SIGNAL_STATE_LABEL } from "./DecisionReason";

const LENSES = ["inside", "outside", "world"] as const;

/** The lens headings. @i18n-keys */
const LENS_HEADING = { inside: "Inside", outside: "Outside", world: "World" } as const;

/** The states a path row names, in loop order. */
const ROW_STATES: readonly CtemSignalState[] = ["new", "to_validate", "watchlist", "case_opened"];

/**
 * One path (UX round 1 B1 on #941): the name on line 1 at full width, never
 * cut; the counts, or "Not configured" with its connect action, on line 2 in
 * one row that does not wrap. Two row heights, aligned across the lenses.
 */
function PathRow({ path, onConnect }: { path: CtemSignalPath; onConnect: () => void }) {
  const { t_i18n } = useFormatter();
  const { count, exactCount } = useNumberFormat();
  return (
    <li className="flex min-w-0 flex-col gap-1 py-1.5 content-compact" data-testid="ctem-trigger-path" data-path={path.id} data-configured={path.configured}>
      <span className="min-w-0 font-medium" data-testid="ctem-trigger-path-name">{t_i18n(path.label)}</span>
      {path.configured ? (
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
          <span className="text-default-secondary" data-testid="ctem-trigger-path-not-configured">{t_i18n("Not configured")}</span>
          <Button variant="ghost" size="sm" onClick={onConnect}>{t_i18n("Connect")}</Button>
        </span>
      )}
    </li>
  );
}

export function TriggerPathsCard({ onConnect }: { onConnect: () => void }) {
  const { t_i18n } = useFormatter();
  const { count } = useNumberFormat();
  const { data, isLoading } = useCtemSignalPaths();
  const paths = data?.paths ?? [];
  const live = paths.filter((p) => p.configured).length;
  return (
    <HeroCard
      title={t_i18n("Signals by trigger path")}
      testId="ctem-trigger-paths"
      chips={
        paths.length > 0 ? (
          <span className="content-caption text-default-secondary tabular-nums">
            {t_i18n("{live} of {total} paths with a source", { live: count(live), total: count(paths.length) })}
          </span>
        ) : null
      }
    >
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
                  <PathRow key={p.id} path={p} onConnect={onConnect} />
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </HeroCard>
  );
}
