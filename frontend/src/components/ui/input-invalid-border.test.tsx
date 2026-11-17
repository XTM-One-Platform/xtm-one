/**
 * Invalid fields and keyboard focus on the Input adapter.
 *
 * The design system draws its red border from its own `error` prop alone; a
 * field marked only with `aria-invalid` kept a transparent border in default
 * mode and only its hint turned red. The adapter now reaches the field from
 * its wrapper in every mode.
 *
 * The rendered tests assert what a user meets (role, name, invalid state,
 * description, type), never class names (frontend/AGENTS.md). The tokens are
 * pinned by source gates on input.tsx and TriggerCard.tsx instead; the painted
 * border and the real Tab focus ring are measured by the UX gate in the lab.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { Input } from "./input";

const inputSource = readFileSync(resolve(__dirname, "./input.tsx"), "utf8");

test.each([
  ["default", false],
  ["compact", true],
])("an invalid %s field is announced invalid with its hint as description", (_mode, compact) => {
  render(
    <>
      <Input
        aria-label="Items in parallel"
        aria-invalid
        aria-describedby="slots-hint"
        compact={compact}
        type="number"
        name="slots"
        value="0"
        onChange={() => {}}
      />
      <p id="slots-hint">Items in parallel must be a whole number from 1 to 10.</p>
    </>,
  );
  const input = screen.getByRole("spinbutton", { name: "Items in parallel" });
  expect(input).toHaveAttribute("aria-invalid", "true");
  expect(input).toHaveAccessibleDescription("Items in parallel must be a whole number from 1 to 10.");
  expect(input).toHaveAttribute("type", "number");
  expect(input).toHaveAttribute("name", "slots");
});

test("a valid field is not announced invalid", () => {
  render(<Input aria-label="Name" name="name" value="ok" onChange={() => {}} />);
  const input = screen.getByRole("textbox", { name: "Name" });
  expect(input).not.toHaveAttribute("aria-invalid", "true");
});

test("input.tsx draws the error border under aria-invalid in every mode, at the full colour", () => {
  expect(inputSource).toMatch(/\[&_input\[aria-invalid=true\]\]:border-feedback-error-primary/);
  expect(inputSource).toMatch(/\[&_input\[aria-invalid=true\]:hover\]:border-feedback-error-primary/);
  // Applied to every Input, not only behind `compact`, and before the caller's classes.
  // The gate reads the arguments of the cn() call that builds the wrapper's className,
  // so a reformat or a renamed tone variable passes and only the rule is held.
  const cnArgs = (inputSource.match(/className=\{cn\(([\s\S]*?)\)\}/)?.[1] ?? "")
    .split(",")
    .map((arg) => arg.trim())
    .filter(Boolean);
  const invalidIndex = cnArgs.findIndex((arg) => /\bINVALID_INPUT_CLASS\b/.test(arg));
  expect(invalidIndex).toBeGreaterThanOrEqual(0);
  // Unconditional: the argument starts with the constant, not `compact && ...` or `compact ? ...`.
  expect(cnArgs[invalidIndex]).toMatch(/^INVALID_INPUT_CLASS\b/);
  expect(invalidIndex).toBeLessThan(cnArgs.indexOf("className"));
  // No faded error colour left (it was /70 in compact mode); other `/NN` opacities are not this rule's business.
  expect(inputSource).not.toMatch(/border-feedback-error-primary\/\d+/);
});

test("TriggerCard sizes its hints with the caption token, not a literal size", () => {
  const source = readFileSync(resolve(__dirname, "../../pages/assignment-detail/TriggerCard.tsx"), "utf8");
  expect(source).not.toMatch(/text-\[9px\]/);
});
