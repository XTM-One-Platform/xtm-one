// SPDX-License-Identifier: Apache-2.0

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/testing/providers";
import { RunAsRow } from "./RunAsRow";

/** Every product source file under src/, test files left out. */
function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.(ts|tsx)$/.test(entry) && !/\.test\.(ts|tsx)$/.test(entry) ? [path] : [];
  });
}

describe("RunAsRow (UX round 4 on #2743, B1)", () => {
  it("is a Runs as row of the grid it sits in, Change named for assistive tech", () => {
    const onChange = vi.fn();
    renderWithProviders(
      <dl>
        <RunAsRow name="SOC lead" changeLabel="Change the run-as user of OpenCRQ" onChange={onChange} testId="run-as" />
      </dl>,
    );
    expect(screen.getByRole("term").textContent).toBe("Runs as");
    expect(screen.getByRole("definition").textContent).toContain("SOC lead");
    expect(screen.getByTestId("run-as").textContent).toBe("SOC lead");
    fireEvent.click(screen.getByRole("button", { name: "Change the run-as user of OpenCRQ" }));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("shows no Change to a reader", () => {
    renderWithProviders(
      <dl>
        <RunAsRow name="SOC lead" changeLabel="Change the run-as user of OpenCRQ" testId="run-as" />
      </dl>,
    );
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("is the only component that renders the run-as label", () => {
    const src = join(process.cwd(), "src");
    const owner = join("src", "components", "module-run-as", "RunAsRow.tsx").split(sep).join("/");
    const writers = sourceFiles(src)
      .filter((path) => /t_i18n\("Runs as(: \{user\})?"/.test(readFileSync(path, "utf8")))
      .map((path) => relative(process.cwd(), path).split(sep).join("/"));
    expect(writers).toEqual([owner]);
  });
});
