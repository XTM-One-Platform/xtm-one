// SPDX-License-Identifier: Apache-2.0
import { describe, expect, test } from "vitest";
import { activeFilterChips, facetValueCounts } from "@/lib/data-table-facets";

interface Row {
  state: string;
  signals: string[];
}

const ROWS: Row[] = [
  { state: "new", signals: ["new_vulnerability", "credential_leak"] },
  { state: "new", signals: ["relevant_threat"] },
  { state: "resolved", signals: ["new_vulnerability"] },
  { state: "remediating", signals: [] },
];
const FILTERS = [
  { key: "status", values: (r: Row) => [r.state] },
  { key: "signal_type", values: (r: Row) => (r.signals.length ? r.signals : ["none"]) },
];

describe("facetValueCounts (#2556)", () => {
  test("with no filter, each value counts the rows that carry it, a row once per value", () => {
    expect(facetValueCounts(ROWS, FILTERS, {})).toEqual({
      status: { new: 2, resolved: 1, remediating: 1 },
      signal_type: { new_vulnerability: 2, credential_leak: 1, relevant_threat: 1, none: 1 },
    });
  });

  test("a facet counts over the rows the OTHER filters keep, never narrowed by its own selection", () => {
    const counts = facetValueCounts(ROWS, FILTERS, { status: ["new"], signal_type: ["relevant_threat"] });
    // Status counts the rows with a relevant threat; Signal type counts the new rows.
    expect(counts.status).toEqual({ new: 1 });
    expect(counts.signal_type).toEqual({ new_vulnerability: 1, credential_leak: 1, relevant_threat: 1 });
  });

  test("picking a value with count N keeps N rows", () => {
    const counts = facetValueCounts(ROWS, FILTERS, { status: ["new", "resolved"] });
    expect(counts.signal_type.new_vulnerability).toBe(2);
  });
});

describe("activeFilterChips (#2556)", () => {
  const filters = [
    { key: "status", label: "Status" },
    { key: "signal_type", label: "Signal type" },
    { key: "owner", label: "Owner" },
  ];
  const options = [
    [
      { value: "new", label: "New" },
      { value: "resolved", label: "Resolved" },
    ],
    [
      { value: "relevant_threat", label: "Relevant new threat or actor" },
      { value: "none", label: "No signal" },
    ],
    [],
  ];

  test("one chip per filter that narrows, its values by label in the options' order", () => {
    expect(activeFilterChips(filters, options, { status: ["resolved", "new"], owner: [], signal_type: ["none"] })).toEqual([
      { key: "status", label: "Status", values: ["New", "Resolved"] },
      { key: "signal_type", label: "Signal type", values: ["No signal"] },
    ]);
  });

  test("a value no option names (a stale URL) still shows, so it can be removed", () => {
    expect(activeFilterChips(filters, options, { owner: ["someone"] })).toEqual([{ key: "owner", label: "Owner", values: ["someone"] }]);
  });

  test("no active filter, no chip", () => {
    expect(activeFilterChips(filters, options, {})).toEqual([]);
  });
});
