/**
 * Unit tests for interactions and visibility - `yarn test:unit`.
 *
 * Handlers run in order on what the click gives; a link carries tokens
 * URL-encoded; another dashboard opens with `form.` tokens; a condition reads
 * tokens, `isSet` and `in`; an unset token makes a condition false.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  clickTokens,
  formTokens,
  handlersOperation,
  isVisible,
  planHandlers,
  readConditions,
  readHandlers,
  readVisibility,
  visibilityForm,
  visibilityOperations,
} from "./interactions.ts";
import { holds } from "./tokens.ts";

const ROW_CLICK = { name: "Name", value: "VPN gateway", row: { name: "VPN gateway", sev: "critical", _url: "https://cti.example/dashboard/id/r1" } };

test("handlers run in order on what the click gives", () => {
  const handlers = readHandlers([
    { type: "drilldown.setToken", options: { tokens: [{ token: "host", key: "row.name.value" }, { token: "label", value: "$host$ ($click.row.sev$)" }] } },
    { type: "drilldown.unsetToken", options: { tokens: [{ token: "q" }] } },
    { type: "drilldown.switchTab", options: { layoutId: "detail" } },
    { type: "drilldown.linkToUrl", options: { url: "https://search.example/?q=$host$&s=$click.row.sev|n$" } },
    { type: "drilldown.evil", options: {} },
  ]);
  assert.equal(handlers.length, 4);
  assert.deepEqual(planHandlers(handlers, ROW_CLICK, { q: "x" }), {
    tokens: [
      ["host", "VPN gateway"],
      ["label", "VPN gateway (critical)"],
      ["q", undefined],
    ],
    tab: "detail",
    link: { url: "https://search.example/?q=VPN%20gateway&s=critical", newTab: false },
  });
});

test("another dashboard opens with form tokens; the platform's page with the row's link", () => {
  const id = "22222222-2222-4222-8222-222222222222";
  const plan = planHandlers(
    readHandlers([{ type: "drilldown.linkToDashboard", options: { dashboardId: id, tokens: [{ token: "host", value: "$click.value$" }, { token: "none", value: "$unset$" }] } }]),
    ROW_CLICK,
    {},
  );
  assert.deepEqual(plan.link, { url: `/dashboards/${id}?form.host=VPN+gateway`, newTab: false });
  assert.deepEqual(planHandlers(readHandlers([{ type: "drilldown.openInPlatform", options: {} }]), ROW_CLICK, {}).link, {
    url: "https://cti.example/dashboard/id/r1",
    newTab: true,
  });
  assert.deepEqual(planHandlers(readHandlers([{ type: "drilldown.openInPlatform", options: {} }]), { value: "3" }, {}, "https://cti.example/w").link, {
    url: "https://cti.example/w",
    newTab: true,
  });
  assert.equal(planHandlers(readHandlers([{ type: "drilldown.linkToDashboard", options: { dashboardId: "../x" } }]), ROW_CLICK, {}).link, undefined);
});

test("a brush sets a time range's two tokens", () => {
  const plan = planHandlers(readHandlers([{ type: "drilldown.setTimeRange", options: { token: "zoom" } }]), { range: { start: "2026-10-04", end: "2026-10-18" } }, {});
  assert.deepEqual(plan.tokens, [
    ["zoom.earliest", "2026-10-04"],
    ["zoom.latest", "2026-10-18"],
  ]);
});

test("the click's own tokens leave the row's link out", () => {
  assert.deepEqual(clickTokens(ROW_CLICK), { "click.name": "Name", "click.value": "VPN gateway", "click.row.name": "VPN gateway", "click.row.sev": "critical" });
});

test("conditions: isSet, in, =, and an unset token is false", () => {
  assert.equal(holds("isSet($host$)", { host: "x" }), true);
  assert.equal(holds("isSet($host$)", {}), false);
  assert.equal(holds('in($sev$, "high", "critical")', { sev: "critical" }), true);
  assert.equal(holds('$sev$ = "low" || $n$ > 3', { sev: "high", n: "4" }), true);
  assert.equal(holds('$sev$ != "low"', {}), false);
});

test("visibility: show and hide conditions, no data", () => {
  const conditions = readConditions({ expressions: { conditions: { picked: { value: "isSet($host$)" }, quiet: { value: '$mode$ = "quiet"' } } } });
  const visibility = readVisibility({ visibility: { showConditions: ["picked"], hideConditions: ["quiet"], hideWhenNoData: true } });
  assert.equal(isVisible(visibility, conditions, { host: "x" }, false), true);
  assert.equal(isVisible(visibility, conditions, {}, false), false);
  assert.equal(isVisible(visibility, conditions, { host: "x", mode: "quiet" }, false), false);
  assert.equal(isVisible(visibility, conditions, { host: "x" }, true), false);
  assert.equal(isVisible(readVisibility({ visibility: { showConditions: ["ghost"] } }), conditions, {}, false), false);
  assert.equal(readVisibility({ visibility: {} }), undefined);
  assert.equal(isVisible(undefined, conditions, {}, true), true);
});

test("form tokens from a dashboard link are bounded", () => {
  assert.deepEqual(formTokens("?form.host=VPN+gateway&form.team=a&form.team=b&other=x&form.1bad=y"), { host: "VPN gateway", team: ["a", "b"] });
  const many = Array.from({ length: 30 }, (_, i) => `form.t${i}=x`).join("&");
  assert.equal(Object.keys(formTokens(many)).length, 20);
});

test("the editor writes a visualization's handlers and its own visibility conditions", () => {
  const definition = {
    visualizations: { kev: { type: "xtm.singlevalue", dataSources: { primary: "s" }, containerOptions: { visibility: { showConditions: ["shared"] } } } },
    expressions: { tokens: { a: "1" }, conditions: { shared: { value: "isSet($team$)" } } },
  };
  assert.deepEqual(visibilityForm(definition, "kev"), { show: "isSet($team$)", hide: "", hideWhenNoData: false });
  const [expressions, viz] = visibilityOperations(definition, "kev", { show: 'isSet($host$)', hide: '$mode$ = "quiet"', hideWhenNoData: true });
  assert.deepEqual(expressions, {
    op: "set",
    path: "expressions",
    value: {
      tokens: { a: "1" },
      conditions: {
        shared: { value: "isSet($team$)" },
        show_kev: { name: "Show kev", value: "isSet($host$)" },
        hide_kev: { name: "Hide kev", value: '$mode$ = "quiet"' },
      },
    },
  });
  assert.deepEqual((viz.value as { containerOptions: unknown }).containerOptions, {
    visibility: { showConditions: ["show_kev"], hideConditions: ["hide_kev"], hideWhenNoData: true },
  });
  const cleared = visibilityOperations(definition, "kev", { show: "", hide: "", hideWhenNoData: false });
  assert.deepEqual(Object.keys((cleared[0].value as { conditions: object }).conditions), ["shared"]);
  const handlers = [{ type: "drilldown.switchTab", options: { layoutId: "two" } }];
  assert.deepEqual(handlersOperation(definition, "kev", handlers).value, { ...definition.visualizations.kev, eventHandlers: handlers });
});
