import { afterEach, beforeEach } from "vitest";

/**
 * A design-system warning fails the test that rendered it.
 *
 * `@filigran/design-system` checks its own contract in development and says so
 * on `console.warn`, tagged with the component: `[Input] rendered without a
 * label…`, `[Dialog] DialogContent was rendered without a DialogTitle…`,
 * `[Menu] MenuTrigger is used without asChild…`. Printed in a passing test's
 * output they were read by nobody - 131 of them stood in the suite until they
 * were counted. Each one is a control with no accessible name or a component
 * used outside its contract, so the test that renders it fails instead.
 *
 * The static guards (`tests_unit/*-accessible-name.test.ts`,
 * `dialog-title.test.ts`) cover what no test renders; this covers what the
 * library alone can see at render time.
 */

const LIBRARY_WARNING = /^\[[A-Z][A-Za-z]+\] /;

/**
 * Warnings the library raises against itself, which no app change can lift.
 * Each names the library finding to remove it with.
 */
const LIBRARY_DEFECTS: ReadonlyArray<{ pattern: RegExp; finding: string }> = [
  {
    // Combobox draws a decorative `<Checkbox presentational />` in each row of
    // a multiple selection, and the Checkbox's own check asks it for a name
    // regardless (LIBRARY-FEEDBACK F28). The app renders no Checkbox itself.
    pattern: /^\[Checkbox\] rendered without a `label` prop/,
    finding: "F28",
  },
];

export function failOnDesignSystemWarnings(): void {
  let seen: string[] = [];
  let original: typeof console.warn;

  beforeEach(() => {
    seen = [];
    original = console.warn;
    console.warn = (...args: unknown[]) => {
      const message = typeof args[0] === "string" ? args[0] : "";
      if (LIBRARY_WARNING.test(message) && !LIBRARY_DEFECTS.some(({ pattern }) => pattern.test(message))) {
        seen.push(message.split("\n")[0]);
      }
      original(...args);
    };
  });

  afterEach(() => {
    console.warn = original;
    if (seen.length) {
      const unique = [...new Set(seen)];
      throw new Error(`the design system warned during this test:\n  ${unique.join("\n  ")}`);
    }
  });
}
