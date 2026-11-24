/**
 * The CTEM deep links into OpenCTI lists (#11954): the FilterGroup the
 * backend counted with reaches OpenCTI in the shape its list URL keeps.
 * `yarn test:unit` (node:test).
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { OPENCTI_VULNERABILITIES_PATH, ctemKpiOpenCtiHref, openCtiListHref, toOpenCtiUrlFilterGroup } from "./opencti-link.ts";

/** The backend's `_F_EXPLOITABLE` (ctem_metrics_collector.py), GraphQL shape. */
const EXPLOITABLE = {
  mode: "or",
  filters: [
    { key: ["x_opencti_cisa_kev"], values: ["true"] },
    { key: ["x_opencti_epss_score"], values: ["0.5"], operator: "gt" },
  ],
  filterGroups: [],
};

/** The tailored exploitable filter: the software inventory AND the exploitable group. */
const TAILORED_EXPLOITABLE = {
  mode: "and",
  filters: [
    {
      key: ["regardingOf"],
      values: [
        { key: "relationship_type", values: ["has"] },
        { key: "id", values: ["software-1", "software-2"] },
      ],
    },
  ],
  filterGroups: [EXPLOITABLE],
};

/**
 * What OpenCTI's list keeps from the URL (opencti-front filtersUtils.tsx,
 * `keepAvailableFilterKeys`): a filter whose `key` is in the available keys
 * by `includes`, so only a string key survives. Mirrored here as the contract.
 */
function keptByOpenCti(group: { filters: { key: unknown }[]; filterGroups: unknown[] }, available: string[]): number {
  const own = group.filters.filter((f) => typeof f.key === "string" && available.includes(f.key)).length;
  return own + (group.filterGroups as never[]).reduce((sum, child) => sum + keptByOpenCti(child, available), 0);
}

const VULNERABILITY_KEYS = ["regardingOf", "x_opencti_cisa_kev", "x_opencti_epss_score", "entity_type"];

const parse = (href: string) => JSON.parse(new URL(href).searchParams.get("filters") as string);

test("a GraphQL list key becomes the one string key OpenCTI's URL reads, operator and mode explicit", () => {
  const group = toOpenCtiUrlFilterGroup(EXPLOITABLE)!;
  assert.deepEqual(group, {
    mode: "or",
    filters: [
      { key: "x_opencti_cisa_kev", values: ["true"], operator: "eq", mode: "or" },
      { key: "x_opencti_epss_score", values: ["0.5"], operator: "gt", mode: "or" },
    ],
    filterGroups: [],
  });
});

test("the tailored exploitable link keeps the software filter and its KEV / EPSS group", () => {
  const href = openCtiListHref("https://cti.example/", OPENCTI_VULNERABILITIES_PATH, TAILORED_EXPLOITABLE)!;
  assert.ok(href.startsWith("https://cti.example/dashboard/arsenal/vulnerabilities?filters="));
  const group = parse(href);
  assert.equal(group.filters[0].key, "regardingOf");
  assert.deepEqual(group.filters[0].values, TAILORED_EXPLOITABLE.filters[0].values);
  assert.equal(group.filterGroups.length, 1);
  assert.equal(group.filterGroups[0].filters.length, 2);
});

test("contract: OpenCTI keeps every filter of the link, none of the raw GraphQL shape", () => {
  for (const raw of [EXPLOITABLE, TAILORED_EXPLOITABLE]) {
    const linked = parse(openCtiListHref("https://cti.example", OPENCTI_VULNERABILITIES_PATH, raw)!);
    const total = raw.filters.length + raw.filterGroups.reduce((sum, child) => sum + child.filters.length, 0);
    assert.equal(keptByOpenCti(linked, VULNERABILITY_KEYS), total);
    // The defect of #11954: the raw shape loses every filter.
    assert.equal(keptByOpenCti(raw as never, VULNERABILITY_KEYS), 0);
  }
});

test("no frontend-only id travels in the link", () => {
  const group = parse(openCtiListHref("https://cti.example", OPENCTI_VULNERABILITIES_PATH, { ...EXPLOITABLE, id: "x" })!);
  assert.equal("id" in group, false);
  assert.equal("id" in group.filters[0], false);
});

test("a filter the URL cannot carry refuses the whole link, never a wider list", () => {
  const multi = { mode: "and", filters: [{ key: ["name", "aliases"], values: ["x"] }], filterGroups: [] };
  assert.equal(openCtiListHref("https://cti.example", OPENCTI_VULNERABILITIES_PATH, multi), null);
  const nested = { mode: "and", filters: [], filterGroups: [{ mode: "xor", filters: [], filterGroups: [] }] };
  assert.equal(openCtiListHref("https://cti.example", OPENCTI_VULNERABILITIES_PATH, nested), null);
  assert.equal(openCtiListHref("https://cti.example", OPENCTI_VULNERABILITIES_PATH, null), null);
});

test("no filter opens the whole list; no OpenCTI URL is no link", () => {
  assert.equal(openCtiListHref("https://cti.example//", OPENCTI_VULNERABILITIES_PATH), "https://cti.example/dashboard/arsenal/vulnerabilities");
  assert.equal(openCtiListHref(null, OPENCTI_VULNERABILITIES_PATH), null);
  assert.equal(openCtiListHref("", OPENCTI_VULNERABILITIES_PATH, EXPLOITABLE), null);
});

test("a vulnerability KPI opens the list it counts, its filter as OpenCTI's URL keeps it", () => {
  const kev = new URL(ctemKpiOpenCtiHref("kev_count", "https://cti.example")!);
  assert.equal(kev.pathname, OPENCTI_VULNERABILITIES_PATH);
  assert.deepEqual(JSON.parse(kev.searchParams.get("filters")!), {
    mode: "and",
    filters: [{ key: "x_opencti_cisa_kev", values: ["true"], operator: "eq", mode: "or" }],
    filterGroups: [],
  });
  const critical = JSON.parse(new URL(ctemKpiOpenCtiHref("critical_count", "https://cti.example")!).searchParams.get("filters")!);
  assert.deepEqual(critical.filters[0], { key: "x_opencti_cvss_base_score", values: ["9"], operator: "gte", mode: "or" });
  // Any other KPI keeps its stage's resource page; no OpenCTI URL is no link.
  assert.equal(ctemKpiOpenCtiHref("detection_coverage_pct", "https://cti.example"), null);
  assert.equal(ctemKpiOpenCtiHref("kev_count", null), null);
});
