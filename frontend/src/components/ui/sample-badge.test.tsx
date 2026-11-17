/**
 * One "Sample" marker - `yarn test`.
 *
 * A greyed preview says it is a sample through `SampleBadge` alone
 * (`dev-docs/ctem-open-source.md` section 6). This file renders the badge and
 * fails when a source file under `src/` renders the "Sample" key any other way:
 * the lowercase `t_i18n("sample")`, or `t_i18n("Sample")` outside
 * `sample-badge.tsx` (a hand-styled span, an outline `Badge`, a second `Chip`).
 */
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

import { SampleBadge } from "@/components/ui/sample-badge";
import { renderWithProviders } from "@/testing/providers";

// vitest runs from frontend/ (CI and `yarn test`), as unavailable-card.test.ts.
const SRC = join(process.cwd(), "src") + sep;
const THE_BADGE = "components/ui/sample-badge.tsx";
const SAMPLE_KEY = /t_i18n\(\s*["'`][Ss]ample["'`]\s*\)/;

function* sources(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      yield* sources(full);
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      yield full;
    }
  }
}

describe("SampleBadge", () => {
  it("renders the shipped Sample key in sentence case", () => {
    renderWithProviders(<SampleBadge />);
    expect(screen.getByText("Sample")).toBeTruthy();
  });

  it("is the only place the Sample key is rendered", () => {
    const offenders: string[] = [];
    let inBadge = 0;
    for (const full of sources(SRC)) {
      const path = full.slice(SRC.length).split(sep).join("/");
      readFileSync(full, "utf8")
        .split("\n")
        .forEach((line, i) => {
          if (!SAMPLE_KEY.test(line)) return;
          if (path === THE_BADGE) inBadge += 1;
          else offenders.push(`${path}:${i + 1}`);
        });
    }
    expect(offenders, 'mark a sample preview with <SampleBadge />, never t_i18n("Sample") or t_i18n("sample")').toEqual([]);
    expect(inBadge).toBe(1);
  });
});
