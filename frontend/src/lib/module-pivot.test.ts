/**
 * Unit tests for the "Open in <Module>" pivot URL (#1357) - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { moduleBaseUrlsOf, modulePivotUrl, pivotModuleOf } from "./module-pivot.ts";

test("an OpenCTI entity opens through the /dashboard/id resolver", () => {
  assert.equal(modulePivotUrl("opencti", "https://cti.example/", "vuln-1"), "https://cti.example/dashboard/id/vuln-1");
  assert.equal(modulePivotUrl("opencti", "https://cti.example/base//", "vuln-1"), "https://cti.example/base/dashboard/id/vuln-1");
});

test("an OpenAEV finding and asset open on their own pages", () => {
  assert.equal(modulePivotUrl("openaev", "https://aev.example", "f-1", "finding"), "https://aev.example/admin/findings/f-1");
  assert.equal(modulePivotUrl("openaev", "https://aev.example", "a-1", "asset"), "https://aev.example/admin/assets/a-1");
});

test("a missing base URL or id gives no link", () => {
  assert.equal(modulePivotUrl("opencti", null, "vuln-1"), null);
  assert.equal(modulePivotUrl("opencti", "", "vuln-1"), null);
  assert.equal(modulePivotUrl("opencti", "https://cti.example", null), null);
  assert.equal(modulePivotUrl("opencti", "https://cti.example", "  "), null);
  assert.equal(modulePivotUrl(null, "https://cti.example", "vuln-1"), null);
});

test("a route the module does not have gives no link, never a guessed one", () => {
  assert.equal(modulePivotUrl("opencti", "https://cti.example", "x", "finding"), null);
  assert.equal(modulePivotUrl("openaev", "https://aev.example", "x", "entity"), null);
  assert.equal(modulePivotUrl("opencrq", "https://crq.example", "x"), null);
});

test("a base URL that is not plain http(s) gives no link", () => {
  assert.equal(modulePivotUrl("opencti", "javascript:alert(1)", "x"), null);
  assert.equal(modulePivotUrl("opencti", "cti.example", "x"), null);
  assert.equal(modulePivotUrl("opencti", "https://user:pw@cti.example", "x"), null);
});

test("the id is encoded into one path segment", () => {
  assert.equal(modulePivotUrl("opencti", "https://cti.example", "a/../b?c"), "https://cti.example/dashboard/id/a%2F..%2Fb%3Fc");
});

test("source keys map to modules, pre-rename identifiers included", () => {
  assert.equal(pivotModuleOf("OpenCTI"), "opencti");
  assert.equal(pivotModuleOf("openbas"), "openaev");
  assert.equal(pivotModuleOf("opengrc"), "opencrq");
  assert.equal(pivotModuleOf("signal"), null);
  assert.equal(pivotModuleOf(undefined), null);
});

test("base URLs are read only from active registrations with a URL (#1986)", () => {
  assert.deepEqual(
    moduleBaseUrlsOf([
      { platform_identifier: "opencti", platform_url: "https://cti.example", status: "active" },
      { platform_identifier: "openaev", platform_url: "https://aev.example", status: "inactive" },
      { platform_identifier: "opencrq", platform_url: null, status: "active" },
      { platform_identifier: "xtm_hub", platform_url: "https://hub.example", status: "active" },
    ]),
    { opencti: "https://cti.example" },
  );
  assert.deepEqual(moduleBaseUrlsOf(undefined), {});
});

test("the canonical registration wins over a legacy one, whatever the order (#1986)", () => {
  const legacy = { platform_identifier: "openbas", platform_url: "https://bas.example", status: "active" };
  const canonical = { platform_identifier: "openaev", platform_url: "https://aev.example", status: "active" };
  assert.deepEqual(moduleBaseUrlsOf([legacy, canonical]), { openaev: "https://aev.example" });
  assert.deepEqual(moduleBaseUrlsOf([canonical, legacy]), { openaev: "https://aev.example" });
  assert.deepEqual(moduleBaseUrlsOf([legacy]), { openaev: "https://bas.example" });
});
