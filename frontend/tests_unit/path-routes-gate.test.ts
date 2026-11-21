// @vitest-environment node
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { describe, it, expect } from "vitest";

/**
 * Path routes, not `?view=` (Sam, 8 Nov 2026, #10203): the path says where you
 * are (page, tab, sub-view, list); the query keeps only state (filters,
 * search, sort, pagination, `?window=`, `?focus=`, `?agent=`, `?vis=`).
 *
 * A fleet's tabs (`/flows/fleets/:fleetId/<tab>`, `/operations/:list`) and the
 * Agentic Flow map's views (`/flows/map/schedule`) moved to path segments.
 * This gate fails when source writes one of them as a query again: a fleet
 * link carrying `?view=`, `?ops=` or `?mode=`, a map link carrying `?view=`,
 * or a link to the former `/flows?tab=`. The tests are exempt.
 *
 * S3b: any page. Tools (`/tools/custom`) and Video Studio (`/videos/media`)
 * moved too; no code reads or writes `?tab=` / `?view=` outside the files of
 * `QUERY_ALLOWED`, each with its reason (a list filter). #797: no redirect
 * shim is left, so none is allowed.
 */

const SRC = path.resolve(__dirname, "../src");

const FORBIDDEN: { name: string; pattern: RegExp }[] = [
  { name: "fleet tab as a query", pattern: /\/flows\/fleets\/[^`"'\s]*\?(?:[^`"'\s]*&)?(?:view|ops|mode)=/ },
  { name: "map view as a query", pattern: /\/flows\/map\?(?:[^`"'\s]*&)?view=/ },
  { name: "Agentic Flow tab as a query", pattern: /\/flows\?tab=/ },
];

/** Files allowed to read or write `?tab=` / `?view=`, each with its reason. */
const QUERY_ALLOWED: Record<string, string> = {
  "pages/operations/WorkItemsTab.tsx": "list filter: the Stale / Waiting / Scheduled segment of the work items list",
};

const QUERY_NAVIGATION = /[?&](?:tab|view)=|\.(?:get|set|has|delete)\(\s*["'](?:tab|view)["']|\{\s*(?:tab|view):\s*key\s*\}/;

/** The code of a line, comments left out (a comment may name the legacy links it replaces). */
function code(line: string): string {
  const trimmed = line.trim();
  if (trimmed.startsWith("//") || trimmed.startsWith("*") || trimmed.startsWith("/*")) return "";
  return line.replace(/\/\/.*$/, "").replace(/\/\*.*?\*\//g, "");
}

async function sourceFiles(dir: string): Promise<string[]> {
  const out: string[] = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await sourceFiles(full)));
    else if (/\.(ts|tsx)$/.test(entry.name) && !/\.test\.(ts|tsx)$/.test(entry.name)) out.push(full);
  }
  return out;
}

describe("path routes gate (#10203)", () => {
  it("writes no fleet tab, map view or Agentic Flow tab as a query", async () => {
    const hits: string[] = [];
    for (const file of await sourceFiles(SRC)) {
      const rel = path.relative(SRC, file).split(path.sep).join("/");
      const lines = (await readFile(file, "utf8")).split("\n");
      lines.forEach((line, i) => {
        for (const { name, pattern } of FORBIDDEN) {
          if (pattern.test(line)) hits.push(`${rel}:${i + 1} ${name}: ${line.trim()}`);
        }
      });
    }
    expect(hits).toEqual([]);
  });

  it("reads or writes ?tab= / ?view= only in a list filter", async () => {
    const hits: string[] = [];
    for (const file of await sourceFiles(SRC)) {
      const rel = path.relative(SRC, file).split(path.sep).join("/");
      if (rel in QUERY_ALLOWED) continue;
      (await readFile(file, "utf8")).split("\n").forEach((line, i) => {
        if (QUERY_NAVIGATION.test(code(line))) hits.push(`${rel}:${i + 1}: ${line.trim()}`);
      });
    }
    expect(hits).toEqual([]);
  });

  it("keeps no exception a file no longer needs", async () => {
    const stale: string[] = [];
    for (const rel of Object.keys(QUERY_ALLOWED)) {
      const lines = (await readFile(path.join(SRC, rel), "utf8")).split("\n");
      if (!lines.some((line) => QUERY_NAVIGATION.test(code(line)))) stale.push(rel);
    }
    expect(stale).toEqual([]);
  });

  it("catches each query navigation form, comments left out", () => {
    expect(QUERY_NAVIGATION.test('navigate("/tools?tab=custom")')).toBe(true);
    expect(QUERY_NAVIGATION.test('const tab = params.get("tab");')).toBe(true);
    expect(QUERY_NAVIGATION.test('q.set("view", next);')).toBe(true);
    expect(QUERY_NAVIGATION.test("setParams({ tab: key })")).toBe(true);
    expect(QUERY_NAVIGATION.test('navigate("/tools/custom?vis=mine")')).toBe(false);
    expect(code("  // a link written before (`?tab=media`) is replaced")).toBe("");
  });

  it("catches each form it forbids", () => {
    const [fleet, map, flows] = FORBIDDEN.map((f) => f.pattern);
    expect(fleet.test("`/flows/fleets/${id}?view=graph`")).toBe(true);
    expect(fleet.test("`/flows/fleets/${id}?ops=runs&review=unreviewed`")).toBe(true);
    expect(fleet.test("`/flows/fleets/${id}?window=7d&view=roi`")).toBe(true);
    expect(fleet.test("`/flows/fleets/${id}/operations/runs?review=unreviewed`")).toBe(false);
    expect(fleet.test("`/flows/fleets/${id}/roi?window=7d`")).toBe(false);
    expect(map.test("\"/flows/map?view=schedule\"")).toBe(true);
    expect(map.test("\"/flows/map/schedule?agent=a\"")).toBe(false);
    expect(flows.test("\"/flows?tab=fleets\"")).toBe(true);
  });
});
