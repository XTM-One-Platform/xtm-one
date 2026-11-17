/**
 * One shown status for an integration on every surface (#11837) - `yarn test`.
 *
 * The stored `status` of an integration only says a credential is there: a
 * typed key nobody tested reads "Not tested", a failed last test "Error", an
 * OAuth row with no token "Not connected". Only `displayedIntegrationStatus`
 * reads that. A surface that looks `INTEGRATION_STATUS[...]` up with the raw
 * stored status shows "Connected" where the detail page one click away reads
 * "Not tested" (UX round 1 B1 of #11843, the user's Objects page). This test
 * fails when a surface does it.
 */
import { expect, test } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join, sep } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
const SRC = join(process.cwd(), "src") + sep;

function* files(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* files(full);
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      yield full;
    }
  }
}

/** A status looked up straight in the table from a row's `status` field. */
const RAW_LOOKUP = /INTEGRATION_STATUS\[\s*[A-Za-z_$][\w$]*\.status\s*\]/;

/** Lookups whose `status` is not an integration's stored status, with why. */
const ALLOWED: Record<string, string> = {
  // `install.status` is already the shown status: providerInstallsStatus().
  [`components${sep}integrations${sep}integration-catalog-browser.tsx`]: "install state carries the displayed status",
  // `person.status` is one person's own credential, not the integration row.
  [`pages${sep}integration-detail${sep}ConnectedAccountsCard.tsx`]: "per-person credential status",
};

test("every surface maps an integration status through displayedIntegrationStatus", () => {
  const offenders: string[] = [];
  for (const path of files(SRC)) {
    const rel = path.slice(SRC.length);
    if (rel in ALLOWED) continue;
    readFileSync(path, "utf8").split("\n").forEach((line, i) => {
      if (RAW_LOOKUP.test(line)) offenders.push(`${rel}:${i + 1}`);
    });
  }
  expect(offenders, "use displayedIntegrationStatus(integration) from @/lib/constants").toEqual([]);
});
