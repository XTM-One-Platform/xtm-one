/**
 * Settings selects keep room for their longest option (#9173, S1c, UX r1 B1 on #9962) - `yarn test`.
 *
 * `max-w-xs` (320 px) cut "Valor predeterminado del despliegue (activado)" in
 * Spanish on the Artificial Intelligence card. The Platform section selects
 * share one width, `max-w-md`, so the column reads aligned and no translated
 * option is truncated.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const FILE = join(process.cwd(), "src", "pages", "settings", "ParametersSection.tsx");

describe("Platform section select width", () => {
  const source = readFileSync(FILE, "utf8");

  it("never narrows a select trigger to max-w-xs", () => {
    expect(source).not.toMatch(/max-w-xs/);
  });

  it("gives every Platform select the same max-w-md width", () => {
    const widths = [...source.matchAll(/className="(max-w-[a-z0-9]+)"/g)].map((m) => m[1]);
    expect(widths.length).toBeGreaterThanOrEqual(3);
    expect(new Set(widths)).toEqual(new Set(["max-w-md"]));
  });
});
