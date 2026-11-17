// @vitest-environment node
import { describe, it, expect } from "vitest";
import path from "node:path";
// @ts-expect-error - a plain .mjs build script, typed by its JSDoc only
import { compileStylesheets, selectorsOf, unresolvedClasses } from "../scripts/fds-migration/compiled-css.mjs";

/**
 * `verify-classes.mjs --compile`, the sandbox's pre-push form of CI's
 * "Design-system guards" step: the CSS comes from compiling `src/index.css`
 * in-process, not from a `yarn build`, so it runs before every push in seconds.
 * An unknown design-system class is caught, a known one passes, and the
 * compile stays well inside the check's 300 s timeout.
 */

const FRONTEND = path.resolve(__dirname, "..");
const used = (tokens: Record<string, string>) => new Map(Object.entries(tokens).map(([token, file]) => [token, [file]]));

describe("verify-classes without a build", () => {
  it("reports a design-system class no selector defines, and only that one", () => {
    const built = selectorsOf([".bg-elevation-default{color:red}.hover\\:text-default-primary\\/50:hover{color:red}"]);
    const missing = unresolvedClasses(
      used({
        "bg-elevation-default": "src/a.tsx",
        "hover:text-default-primary/50": "src/b.tsx",
        "bg-elevation-made-up-tone": "src/c.tsx",
        "flex-1": "src/d.tsx",
        "bg-elevation-default-layer-0": "src/e.tsx",
      }),
      built,
    );
    expect(missing).toEqual([{ token: "bg-elevation-made-up-tone", count: 1, example: "src/c.tsx" }]);
  });

  it("compiles src/index.css in-process: a class the design system emits resolves, an invented one is caught", async () => {
    const started = Date.now();
    const built = selectorsOf(await compileStylesheets(FRONTEND));
    const seconds = (Date.now() - started) / 1000;
    expect(built.size).toBeGreaterThan(1000);
    const missing = unresolvedClasses(
      used({ "bg-elevation-default": "src/a.tsx", "text-feedback-negative-primary": "src/b.tsx" }),
      built,
    );
    expect(missing.map((m: { token: string }) => m.token)).toEqual(["text-feedback-negative-primary"]);
    // The check's timeout is 300 s; a compile that needs more than a minute is a build again.
    expect(seconds).toBeLessThan(60);
  }, 120_000);
});
