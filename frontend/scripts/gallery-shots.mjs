/**
 * Captures the primitive gallery (`/gallery.html`) in both themes.
 *
 * Run it once before a styling change and once after, into two different
 * directories, and diff the pairs: it is the only check that answers whether a
 * token remapping preserved what it claimed to. Types, lint and unit tests are
 * all silent on a surface that lost its contrast.
 *
 *   node scripts/gallery-shots.mjs --out ../.visual/before
 *
 * Expects a dev server already listening (yarn dev).
 */
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const args = process.argv.slice(2);
const outIndex = args.indexOf("--out");
const outDir = path.resolve(outIndex === -1 ? ".visual/current" : args[outIndex + 1]);
const baseUrl = process.env.GALLERY_BASE_URL ?? "http://localhost:3100";

await mkdir(outDir, { recursive: true });

const browser = await chromium.launch();
// A fixed viewport width matters more than its height: the gallery is captured
// full-page, so only the width decides how its grid wraps. A capture taken at a
// different width is not comparable with the reference set.
const page = await browser.newPage({ viewport: { width: 1600, height: 1200 } });

await page.goto(`${baseUrl}/gallery.html`, { waitUntil: "networkidle" });

for (const theme of ["dark", "light"]) {
  await page.getByRole("button", { name: theme === "dark" ? "Dark" : "Light" }).click();
  // Colour transitions are declared on most surfaces; without settling them the
  // capture catches a mid-transition frame and every diff shows noise.
  await page.waitForTimeout(400);
  const file = path.join(outDir, `gallery-${theme}.png`);
  await page.screenshot({ path: file, fullPage: true });
  console.log(`wrote ${path.relative(process.cwd(), file)}`);

  // The shell artboard on its own. It is one small region of a very tall page,
  // and the geometry it proves — the 68px bar, the 180px rail, the gradient
  // surface shared by both — is unreadable at full-page scale.
  const shell = page.locator("nav").first().locator("..");
  const shellFile = path.join(outDir, `shell-${theme}.png`);
  await shell.screenshot({ path: shellFile });
  console.log(`wrote ${path.relative(process.cwd(), shellFile)}`);
}

await browser.close();
