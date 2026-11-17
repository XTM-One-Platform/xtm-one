/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The layout check of one deck, held ABOVE the panel that shows it.
 *
 * A check renders the whole deck in the headless browser and measures every
 * slide — seconds on a real deck. `PresentationLayoutPanel` used to own the
 * request, and the viewer unmounts the panel when it is hidden: closing it to
 * look at the deck while it rendered lost the run, showed nothing anywhere,
 * and re-opening the panel rendered the same revision a second time. The
 * viewer calls this hook once instead, spins the toolbar button for as long as
 * a check is in flight — panel open or not — and hands the run to the panel,
 * which asks for a check when it opens with nothing current to show and
 * otherwise displays the report that arrived while it was closed.
 *
 * Everything the hook exposes is scoped to the deck it was asked about: the
 * router reuses the viewer when only `:id` changes (a duplicate lands on its
 * copy), and a report, an error or a pending flag must never be read against
 * another deck.
 */
import { useCallback, useRef, useState } from "react";

import { useCheckPresentationLayout } from "@/hooks/api/usePresentationApi";
import type { Presentation, PresentationLayoutCheck } from "@/types";

/** The layout check of one deck as the viewer holds it. */
export interface LayoutCheckRun {
  /** The last report for THIS deck; `null` before the first success, while a check runs and after a failed one. */
  report: PresentationLayoutCheck | null;
  /** A check of this deck is in flight. */
  isPending: boolean;
  /** The last check of this deck failed; `error` says how. */
  isError: boolean;
  error: unknown;
  /** The revision `report` describes. */
  checkedRevision: number | null;
  /** The deck has been saved since `report` was measured. */
  stale: boolean;
  /** Render the deck again and measure it — a no-op while a check of this deck is in flight. */
  run: () => void;
}

export function useLayoutCheckRun(presentation: Pick<Presentation, "id" | "revision"> | undefined): LayoutCheckRun {
  const { mutate, isPending, isError, error } = useCheckPresentationLayout();
  const id = presentation?.id;
  const revision = presentation?.revision;
  // The deck the last request was for, and the report the last check produced
  // WITH the deck + revision it describes — recorded together when the check
  // SUCCEEDS, never when it is requested, so a check that fails leaves no
  // revision behind that no report describes. The report is kept here rather
  // than read off the mutation: `useMutation` exposes the latest call's `data`,
  // which lands in a different state update than the revision, and a revision
  // paired with a report it does not describe would flag the wrong report
  // stale. The report itself says which revision the render measured (the
  // render loads the deck's current row, which a save landing meanwhile may
  // have moved on); the revision read when the request left is the fallback
  // for a report that does not say. A later save makes the report stale either
  // way.
  const [requestedFor, setRequestedFor] = useState<string | null>(null);
  const [checked, setChecked] = useState<{ id: string; revision: number; report: PresentationLayoutCheck } | null>(
    null,
  );
  // The deck whose check is in flight, held in a ref because `isPending` only
  // flips on the render AFTER `mutate` — two callers in the same tick (the
  // panel's mount-run and a click) would both read it false and render the
  // deck twice, and `useMutation` keeps the callbacks of the latest call only,
  // so the first render's report would never be recorded. A `run` for the deck
  // already being measured is therefore a no-op; another deck's request goes
  // through, since the superseded one can no longer report anyway.
  const inFlight = useRef<string | null>(null);

  const run = useCallback(() => {
    if (id === undefined || revision === undefined) return;
    if (inFlight.current === id) return;
    inFlight.current = id;
    const requested = revision;
    setRequestedFor(id);
    // Nothing is measured until the new request lands: the report on hand is
    // superseded, and a request that fails must not leave it looking current.
    setChecked(null);
    mutate(id, {
      onSuccess: (report) => setChecked({ id, revision: report.revision ?? requested, report }),
      onSettled: () => {
        if (inFlight.current === id) inFlight.current = null;
      },
    });
  }, [id, mutate, revision]);

  const forThisDeck = id !== undefined && requestedFor === id;
  const measured = forThisDeck && checked !== null && checked.id === id ? checked : null;
  return {
    report: measured?.report ?? null,
    isPending: forThisDeck && isPending,
    isError: forThisDeck && isError,
    error: forThisDeck ? error : null,
    checkedRevision: measured?.revision ?? null,
    stale: measured !== null && measured.revision !== revision,
    run,
  };
}
