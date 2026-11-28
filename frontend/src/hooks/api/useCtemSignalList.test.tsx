// SPDX-License-Identifier: Apache-2.0

// The Signals page reads every page of a state (#2767). Asking the next page while a background
// refetch ran cancelled that refetch, whose restart asked again: every loaded page was re-read
// about once a second until the tab ran out of memory. The next page now waits for an idle query,
// and the full read polls on a slow floor (a refetch re-reads every page).

import { describe, expect, it } from "vitest";

import {
  SIGNAL_FULL_READ_POLL_MS,
  nextSignalPageOffset,
  shouldLoadNextSignalPage,
  type CtemSignal,
  type CtemSignalList,
} from "@/hooks/api/useCtemSignalsApi";

const page = (n: number, total: number): CtemSignalList => ({
  total,
  counts: {},
  signals: Array.from({ length: n }, (_, i) => ({ id: `s-${i}` }) as unknown as CtemSignal),
});

describe("the full signal read (#2767)", () => {
  it("asks the next page only while the query is idle", () => {
    expect(shouldLoadNextSignalPage({ hasNextPage: true, isFetching: false, isError: false })).toBe(true);
    // A background refetch (the poll or a stream frame) is never cancelled by a next page.
    expect(shouldLoadNextSignalPage({ hasNextPage: true, isFetching: true, isError: false })).toBe(false);
    expect(shouldLoadNextSignalPage({ hasNextPage: true, isFetching: false, isError: true })).toBe(false);
    expect(shouldLoadNextSignalPage({ hasNextPage: false, isFetching: false, isError: false })).toBe(false);
  });

  it("stops once every signal of the total is loaded", () => {
    expect(nextSignalPageOffset(page(200, 450), [page(200, 450)])).toBe(200);
    expect(nextSignalPageOffset(page(50, 450), [page(200, 450), page(200, 450), page(50, 450)])).toBeUndefined();
    // An empty page ends the read even when the total moved under it.
    expect(nextSignalPageOffset(page(0, 900), [page(200, 900), page(0, 900)])).toBeUndefined();
  });

  it("polls a full read far slower than the cases floor", () => {
    expect(SIGNAL_FULL_READ_POLL_MS).toBeGreaterThanOrEqual(5 * 60_000);
  });
});
