// One shared disclosure line: a hand-written <summary> showed the browser's
// 1 px focus outline across the whole card and a 10 px, 15 px tall target.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DisclosureSummary } from "./disclosure";

const SRC = join(process.cwd(), "src");

/** Files allowed to write a raw <summary>: the shared component, and the report's own stylesheet-driven blocks. */
const ALLOWED = new Set([
  ["components", "ui", "disclosure.tsx"].join(sep),
  ["components", "report", "blocks.tsx"].join(sep),
]);

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx$/.test(name) && !/\.test\.tsx$/.test(name) ? [path] : [];
  });
}

describe("DisclosureSummary", () => {
  it("renders a native summary inside its details", () => {
    render(
      <details>
        <DisclosureSummary>Details</DisclosureSummary>
        <p>body</p>
      </details>,
    );
    const summary = screen.getByText("Details");
    expect(summary.tagName).toBe("SUMMARY");
    // The disclosure keeps the native element, so Enter and Space still toggle it.
    expect(summary.parentElement?.tagName).toBe("DETAILS");
  });

  it("is the only <summary> in the app outside the report blocks", () => {
    const offenders = sources(SRC)
      .filter((path) => !ALLOWED.has(relative(SRC, path)))
      .filter((path) => /<summary[\s>]/.test(readFileSync(path, "utf8")))
      .map((path) => relative(SRC, path));
    expect(offenders).toEqual([]);
  });
});
