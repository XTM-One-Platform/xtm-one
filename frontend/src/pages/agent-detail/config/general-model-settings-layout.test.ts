/**
 * The model settings of the agent General form (#11694): Temperature and Max
 * Iterations share one row, then Reasoning Effort, Organization context and
 * Preloaded organization categories share the next one. The DOM order is the
 * reading order (left to right, then down), so keyboard focus follows it.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const source = readFileSync(join(__dirname, "GeneralSection.tsx"), "utf8");

function at(needle: string): number {
  const index = source.indexOf(needle);
  expect(index, needle).toBeGreaterThan(-1);
  return index;
}

describe("agent General form: compact model settings (#11694)", () => {
  it("lays the two sliders on one row, then the three selects on the next, in reading order", () => {
    const sliders = at('data-testid="agent-general-sliders"');
    const temperature = at("id={TEMPERATURE_FIELD_ID}");
    const maxIterations = at("id={MAX_ITERATIONS_FIELD_ID}");
    const selects = at('data-testid="agent-general-selects"');
    const reasoning = at("id={REASONING_EFFORT_FIELD_ID}");
    const mode = at("<OrganizationContextModeSelect");
    const categories = at("<OrganizationContextCategoriesSelect");
    expect([sliders, temperature, maxIterations, selects, reasoning, mode, categories]).toEqual(
      [sliders, temperature, maxIterations, selects, reasoning, mode, categories].slice().sort((a, b) => a - b),
    );
  });
});
