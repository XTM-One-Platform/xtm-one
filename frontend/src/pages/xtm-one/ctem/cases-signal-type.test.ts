// SPDX-License-Identifier: Apache-2.0
import { describe, expect, test } from "vitest";
import { CASE_SIGNAL_TYPES, NO_SIGNAL, caseSignalTypeOptions, caseSignalTypes } from "./cases-signal-type";

describe("the Signal type filter of the cases list (#2556)", () => {
  test("a case answers its trigger paths, or 'none' when its reader sees no signal", () => {
    expect(caseSignalTypes({ signal_types: ["credential_leak", "new_vulnerability"] })).toEqual(["credential_leak", "new_vulnerability"]);
    expect(caseSignalTypes({ signal_types: [] })).toEqual([NO_SIGNAL]);
    expect(caseSignalTypes({})).toEqual([NO_SIGNAL]);
  });

  test("the options are the 12 trigger paths in the model's order, then No signal", () => {
    const options = caseSignalTypeOptions((key) => key, undefined, "No signal");
    expect(options.map((o) => o.value)).toEqual([...CASE_SIGNAL_TYPES, NO_SIGNAL]);
    expect(options).toHaveLength(13);
    expect(options[0].label).toBe("New vulnerability on a running version");
  });

  test("a path a newer backend names is offered with the server's label, before No signal", () => {
    const options = caseSignalTypeOptions((key) => key, { new_vulnerability: "x", future_path: "Future path" }, "No signal");
    expect(options.slice(-2)).toEqual([
      { value: "future_path", label: "Future path" },
      { value: NO_SIGNAL, label: "No signal" },
    ]);
  });
});
