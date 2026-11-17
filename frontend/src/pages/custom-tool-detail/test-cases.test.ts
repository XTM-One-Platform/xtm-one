import { describe, expect, it } from "vitest";
import { TEST_CASES_EXAMPLE, parseTestCases } from "./test-cases";

describe("parseTestCases", () => {
  it("reads blank text as no case", () => {
    expect(parseTestCases("  \n")).toEqual({ ok: true, cases: [] });
  });

  it("reads the example it offers as a placeholder", () => {
    const parsed = parseTestCases(TEST_CASES_EXAMPLE);
    expect(parsed.ok).toBe(true);
    expect(parsed.ok && parsed.cases.map((c) => c.name)).toEqual([
      "dry run on example.com",
      "refuses an empty domain",
    ]);
  });

  it("refuses what is not a list of named cases", () => {
    expect(parseTestCases("{")).toEqual({ ok: false, error: "Invalid JSON. Please check your input." });
    expect(parseTestCases('{"name": "a"}')).toEqual({ ok: false, error: "Data cases must be a JSON list." });
    expect(parseTestCases('[{"inputs": {}}]')).toEqual({ ok: false, error: "Every data case needs a name." });
    expect(parseTestCases('[{"name": "  "}]')).toEqual({ ok: false, error: "Every data case needs a name." });
  });
});
