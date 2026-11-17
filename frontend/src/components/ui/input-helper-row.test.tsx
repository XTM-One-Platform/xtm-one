/**
 * The helper row of the Input adapter: the help or error line and the `N/max`
 * character counter the design system draws under a field with `maxLength`.
 *
 * The library sets both at its `text-1` step (10 px), under the 12 px floor
 * of the design standards; the adapter lifts the row to the compact body
 * step on every Input. The rendered tests assert what a user meets (the
 * counter text and the field's description), never class names
 * (frontend/AGENTS.md); the rule itself is pinned by a source gate on
 * input.tsx, and the painted size is measured by the UX gate in the lab.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { Input } from "./input";

const inputSource = readFileSync(resolve(__dirname, "./input.tsx"), "utf8");

test("a field with maxLength shows its counter and is described by it", () => {
  render(<Input label="Title" maxLength={300} value="Quarterly review" onChange={() => {}} />);
  const input = screen.getByRole("textbox", { name: "Title" });
  expect(screen.getByText("16/300")).toBeInTheDocument();
  expect(input).toHaveAccessibleDescription(/16\/300/);
});

test("input.tsx sets the helper row at the compact body step on every Input", () => {
  // The row is the library's `<input id>-helper` element; both of its lines are reached.
  expect(inputSource).toMatch(/\[&_\[id\$=-helper\]>span\]:content-compact/);
  const cnArgs = (inputSource.match(/className=\{cn\(([\s\S]*?)\)\}/)?.[1] ?? "")
    .split(",")
    .map((arg) => arg.trim())
    .filter(Boolean);
  const helperIndex = cnArgs.findIndex((arg) => /\bHELPER_ROW_CLASS\b/.test(arg));
  // Unconditional, and before the caller's classes so a call site can still override it.
  expect(helperIndex).toBeGreaterThanOrEqual(0);
  expect(cnArgs[helperIndex]).toBe("HELPER_ROW_CLASS");
  expect(helperIndex).toBeLessThan(cnArgs.indexOf("className"));
  // Never the library's 10 px step or a literal size under the floor.
  expect(inputSource).not.toMatch(/\[id\$=-helper\][^"]*:(content-caption|text-1\b|text-\[(?:[0-9]|1[01])px\])/);
});
