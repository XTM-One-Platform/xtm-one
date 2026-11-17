/**
 * Runs axe against the primitive gallery, in both themes, in a real browser.
 *
 *   node scripts/gallery-axe.mjs            # report
 *   node scripts/gallery-axe.mjs --check    # non-zero exit on any violation
 *
 * A real browser rather than jsdom, on purpose: the rules worth having here are
 * `color-contrast` and `focus-visible`, and neither can be evaluated without
 * layout and computed styles. In jsdom axe silently skips them and reports a
 * clean run, which is worse than not running it.
 *
 * Scope: the gallery, so this measures the design system's primitives as this
 * app renders them — the tokens, the themes and the type scale — not the app's
 * own screens. It is the accessibility counterpart to `gallery-shots.mjs`, and
 * the same caveat applies: a page the app does not have cannot be covered here.
 *
 * Expects a dev server already listening (yarn dev).
 */
import { createRequire } from "node:module";
import { chromium } from "playwright";

const require = createRequire(import.meta.url);
const axePath = require.resolve("axe-core");
const check = process.argv.includes("--check");
const baseUrl = process.env.GALLERY_BASE_URL ?? "http://localhost:3100";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1200 } });
await page.goto(`${baseUrl}/gallery.html`, { waitUntil: "networkidle" });

let total = 0;

for (const theme of ["dark", "light"]) {
  await page.getByRole("button", { name: theme === "dark" ? "Dark" : "Light" }).click();
  await page.waitForTimeout(400);
  // Hover the primary button before scanning. Interactive states are where a
  // contrast failure hides — a colour is chosen for the resting surface and the
  // hover surface is picked for feel — and the one real violation this script
  // ever found was caught only because its own theme click left a button
  // active. Making that deliberate turns luck into coverage.
  const primary = page.getByRole("button", { name: theme === "dark" ? "Dark" : "Light" });
  await primary.hover();
  await page.waitForTimeout(200);

  await page.addScriptTag({ path: axePath });

  const results = await page.evaluate(async () => {
    // @ts-expect-error — injected by addScriptTag
    return window.axe.run(document, {
      resultTypes: ["violations"],
      // The gallery is a page of specimens, so landmark and heading-order rules
      // fire on the harness rather than on the components under test.
      rules: {
        region: { enabled: false },
        "heading-order": { enabled: false },
      },
    });
  });

  console.log(`\n── ${theme} ──`);
  if (results.violations.length === 0) {
    console.log("no violations");
    continue;
  }
  for (const violation of results.violations) {
    total += violation.nodes.length;
    console.log(`${violation.impact?.toUpperCase() ?? "?"}  ${violation.id} — ${violation.help}`);
    for (const node of violation.nodes.slice(0, 4)) {
      console.log(`      ${node.target.join(" ")}`);
      const summary = node.failureSummary?.split("\n").filter(Boolean).slice(1, 3) ?? [];
      for (const line of summary) console.log(`        ${line.trim()}`);
    }
    if (violation.nodes.length > 4) {
      console.log(`      … and ${violation.nodes.length - 4} more node(s)`);
    }
  }
}

await browser.close();

console.log(`\n${total} failing node(s) across both themes`);
if (check && total > 0) process.exit(1);
