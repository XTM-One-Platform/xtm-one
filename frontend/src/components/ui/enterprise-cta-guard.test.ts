/**
 * One call-to-action system - `yarn test`.
 *
 * A page says "Start free trial" through `EnterpriseCtaButtons` or
 * `AiLockedFooter` (`components/ui/enterprise-upsell.tsx`), never through its
 * own button (`dev-docs/ctem-open-source.md` section 6). This file fails when a
 * source file under `src/pages` imports `XTM_ONE_TRIAL_URL` or renders the
 * "Start free trial" key outside the frozen allowlist of the four pages that
 * already did on main when the rule was written. The allowlist only shrinks:
 * a page migrated to the shared buttons is dropped from it in the same change.
 */
import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`), as sample-badge.test.tsx.
const PAGES = join(process.cwd(), "src", "pages") + sep;
const ALLOWLIST = [
  "LicenseRequired.tsx",
  "settings/ParametersSection.tsx",
  "settings/QuotasSection.tsx",
  "xtm-one/CtemTrialSplash.tsx",
];
const OWN_CTA = /XTM_ONE_TRIAL_URL|start free trial/i;

function* sources(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      yield* sources(full);
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      yield full;
    }
  }
}

function pagesWithOwnCta(): string[] {
  const found: string[] = [];
  for (const full of sources(PAGES)) {
    if (OWN_CTA.test(readFileSync(full, "utf8"))) {
      found.push(full.slice(PAGES.length).split(sep).join("/"));
    }
  }
  return found.sort();
}

describe("Enterprise call to action", () => {
  it("is rendered by the shared buttons on every page outside the frozen allowlist", () => {
    const offenders = pagesWithOwnCta().filter((p) => !ALLOWLIST.includes(p));
    expect(offenders, "use EnterpriseCtaButtons or AiLockedFooter, never a page's own trial button").toEqual([]);
  });

  it("keeps the allowlist to the pages that still need it", () => {
    const found = pagesWithOwnCta();
    const stale = ALLOWLIST.filter((p) => !found.includes(p));
    expect(stale, "drop a migrated page from ALLOWLIST in the same change").toEqual([]);
  });
});
