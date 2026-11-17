// @vitest-environment node
import { existsSync, readdirSync } from "node:fs";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * The app and the design system must share ONE copy of each Radix primitive.
 *
 * Radix keeps its layer stack (which dialog is on top, which one Escape and an
 * outside click belong to) and its tooltip provider in module state. With the
 * app's pins older than the library's ranges, Yarn installed the library a
 * private copy under its own node_modules, and two stacks that do not know about
 * each other: Escape in the deck's shortcut sheet (the app's Radix) also closed
 * the template preview around it (the library's), which main's end-to-end
 * tests caught. Nothing else shows it — every dialog still opens and closes on
 * its own. So the fix, pinning the app's Radix packages to the versions the
 * library resolves, is held here: a library bump that outgrows the pins puts the
 * private copy back and fails this test instead of a user's Escape key.
 */
describe("Radix copies", () => {
  it("installs no private Radix under the design system", () => {
    const nested = path.resolve(__dirname, "../node_modules/@filigran/design-system/node_modules/@radix-ui");
    const copies = existsSync(nested) ? readdirSync(nested) : [];
    expect(copies).toEqual([]);
  });
});
