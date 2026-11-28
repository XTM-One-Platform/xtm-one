// SPDX-License-Identifier: Apache-2.0

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/testing/providers";
import { RunAsLine } from "./RunAsLine";

/** Every product source file under src/, test files left out. */
function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.(ts|tsx)$/.test(entry) && !/\.test\.(ts|tsx)$/.test(entry) ? [path] : [];
  });
}

describe("RunAsLine (UX round 1 on #2742, B1)", () => {
  it("names Change for assistive tech", () => {
    const onChange = vi.fn();
    renderWithProviders(<RunAsLine name="SOC lead" changeLabel="Change the run-as user of OpenCRQ" onChange={onChange} testId="run-as" />);
    expect(screen.getByTestId("run-as").textContent).toContain("SOC lead");
    const change = screen.getByRole("button", { name: "Change the run-as user of OpenCRQ" });
    fireEvent.click(change);
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("shows no Change to a reader", () => {
    renderWithProviders(<RunAsLine name="SOC lead" changeLabel="Change the run-as user of OpenCRQ" testId="run-as" />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("is the only component that renders the run-as line", () => {
    const src = join(process.cwd(), "src");
    const owner = join("src", "components", "module-run-as", "RunAsLine.tsx");
    const writers = sourceFiles(src)
      .filter((path) => readFileSync(path, "utf8").includes('"Runs as: {user}"'))
      .map((path) => relative(process.cwd(), path).split(sep).join("/"));
    expect(writers).toEqual([owner.split(sep).join("/")]);
  });
});
