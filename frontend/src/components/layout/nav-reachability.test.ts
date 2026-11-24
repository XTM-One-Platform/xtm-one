/**
 * Reachability guard of the left navigation - `yarn test:unit` (node:test).
 *
 * Slice N1 of the left-menu consolidation (#9301): before any entry moves,
 * this file pins WHO reaches WHICH page from the nav and the ⌘K palette, in
 * which lock state, behind which route guard, on today's model. The later
 * slices (N2a: model and tree, N2b: renames, N3: family pages) regroup the
 * rows; they must keep every line of `REACHABILITY` true. A destination lost
 * for one role, mode, flag set or AI state fails here with the combination
 * named; a destination added updates the table in the same PR, on purpose.
 *
 * The table is written by hand, not generated: it is the reviewable contract
 * of the program ("no entry disappears for a role that sees it today"), and
 * a generated snapshot would be rewritten by the change it guards.
 *
 * N2a added the one new destination, `/fleets` (a tab of Agentic Flow
 * until then), locked `unlicensed` on Community Edition like Value; every other
 * line is N1's, unchanged. N2c adds `/agents-hub`, the Agents group's hub
 * (open to everyone, no lock: its cards carry their own), and moves `/fleets`
 * to its own route. #797: no former address redirects (tested below).
 * N3 lists `/observability/outcomes`, a tab of Analytics until then, as a page
 * of the Observability family (administrators, as its tab was).
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { navDestinations, resolveNav, type NavFlags } from "./nav-model.ts";

type Role = "admin" | "groupManager" | "member";

/** The flags that hide or show a nav entry (the mode and the AI state are enumerated apart). */
type Gate =
  | "chat"
  | "customVariables"
  | "relays"
  | "aiAdoption"
  | "deepInvestigation"
  | "videoStudio"
  | "dashboardStudio"
  | "evaluation"
  | "health";

const GATES: readonly Gate[] = [
  "chat",
  "customVariables",
  "relays",
  "aiAdoption",
  "deepInvestigation",
  "videoStudio",
  "dashboardStudio",
  "evaluation",
  "health",
];

/** The role check of the route itself, read from `App.tsx` (the nav alone is not access control). */
type Guard = "AdminRoute" | "AnalyticsRoute" | "none";

interface Reach {
  to: string;
  /** Who gets the entry in the nav and the palette. */
  roles: readonly Role[];
  /** Listed only while this flag is on. */
  gate?: Gate;
  /** "ai": an AI area, locked `ai_disabled` when AI is off, else `unlicensed`
   *  without a licence (FR-017, FR-031). "license": an Enterprise read surface,
   *  locked `unlicensed` on Community Edition only, open with AI off. */
  lock?: "ai" | "license";
  guard: Guard;
  /** Listed in XTM One mode only: domain content of the XTM platforms (Intents, #445; CTEM cases, #1135). */
  xtmOneOnly?: true;
}

const EVERYONE: readonly Role[] = ["admin", "groupManager", "member"];
const ADMIN: readonly Role[] = ["admin"];
const ADMIN_AND_GROUP_MANAGERS: readonly Role[] = ["admin", "groupManager"];

/** Today's left menu (main 667ec49f), one line per destination; the place of a row is not part of the contract. */
const REACHABILITY: readonly Reach[] = [
  { to: "/", roles: EVERYONE, guard: "none" },
  { to: "/chat", roles: EVERYONE, gate: "chat", lock: "ai", guard: "none" },
  // The record of the CTEM loop (#1135): every user, open with AI off; the route sits behind XtmOneRoute, a mode guard.
  { to: "/ctem/cases", roles: EVERYONE, guard: "none", xtmOneOnly: true },
  // The signals and their sources (#1401): read by every user, behind the same mode guard.
  { to: "/ctem/signals", roles: EVERYONE, guard: "none", xtmOneOnly: true },
  // The inventory (#1178): read by every user; editing stays with administrators on the API.
  { to: "/ctem/inventory", roles: EVERYONE, guard: "none", xtmOneOnly: true },
  { to: "/flows", roles: EVERYONE, guard: "none" },
  { to: "/fleets", roles: EVERYONE, lock: "license", guard: "none" },
  { to: "/agents", roles: EVERYONE, lock: "ai", guard: "none" },
  { to: "/agents-hub", roles: EVERYONE, guard: "none" },
  { to: "/work", roles: EVERYONE, guard: "none" },
  { to: "/tasks", roles: EVERYONE, guard: "none" },
  { to: "/operations", roles: EVERYONE, guard: "none" },
  { to: "/presentations", roles: EVERYONE, lock: "ai", guard: "none" },
  { to: "/videos", roles: EVERYONE, gate: "videoStudio", lock: "ai", guard: "none" },
  { to: "/dashboards", roles: EVERYONE, gate: "dashboardStudio", guard: "none" },
  { to: "/files", roles: EVERYONE, guard: "none" },
  { to: "/agent-studio", roles: EVERYONE, lock: "ai", guard: "none" },
  { to: "/agents/new", roles: EVERYONE, lock: "ai", guard: "none" },
  { to: "/knowledge", roles: EVERYONE, lock: "ai", guard: "none" },
  { to: "/prompts", roles: EVERYONE, lock: "ai", guard: "none" },
  { to: "/templates", roles: EVERYONE, lock: "ai", guard: "none" },
  { to: "/skills", roles: EVERYONE, lock: "ai", guard: "none" },
  { to: "/investigation-packs", roles: EVERYONE, gate: "deepInvestigation", lock: "ai", guard: "none" },
  { to: "/tools-and-integrations", roles: EVERYONE, guard: "none" },
  { to: "/mcp-servers", roles: EVERYONE, lock: "ai", guard: "none" },
  { to: "/tools", roles: EVERYONE, guard: "none" },
  { to: "/integrations", roles: EVERYONE, guard: "none" },
  { to: "/connections", roles: EVERYONE, guard: "none" },
  { to: "/connections/variables", roles: EVERYONE, gate: "customVariables", guard: "none" },
  { to: "/connections/relays", roles: EVERYONE, gate: "relays", guard: "none" },
  { to: "/observability", roles: ADMIN_AND_GROUP_MANAGERS, guard: "AnalyticsRoute" },
  // The page reads Model usage for a group manager whatever the URL says.
  { to: "/observability/outcomes", roles: ADMIN, guard: "AnalyticsRoute" },
  { to: "/observability/traces", roles: ADMIN, guard: "AdminRoute" },
  { to: "/observability/sessions", roles: ADMIN, guard: "AdminRoute" },
  { to: "/ai-adoption", roles: ADMIN, gate: "aiAdoption", lock: "ai", guard: "AdminRoute" },
  { to: "/evaluations", roles: ADMIN, gate: "evaluation", lock: "ai", guard: "AdminRoute" },
  { to: "/health", roles: ADMIN, gate: "health", lock: "ai", guard: "AdminRoute" },
  // No role guard: the nav alone keeps Intents and Groups to administrators
  // (#9301 UX r1 A4, r2 A5). Intents are XTM One mode only (#445): the route
  // sits behind XtmOneRoute, which is a mode guard, not a role guard.
  { to: "/intents", roles: ADMIN, lock: "ai", guard: "none", xtmOneOnly: true },
  { to: "/users-and-groups", roles: ADMIN, guard: "AdminRoute" },
  { to: "/users-and-groups/groups", roles: ADMIN, guard: "none" },
  { to: "/objects", roles: ADMIN, guard: "AdminRoute" },
  { to: "/logs", roles: ADMIN, guard: "AdminRoute" },
  { to: "/settings", roles: ADMIN, guard: "AdminRoute" },
];

const ROLES: readonly Role[] = ["admin", "groupManager", "member"];

/** AI on, AI switched off (licensed), Community Edition (AI on, no licence), and both off. */
const AI_STATES: readonly { name: string; aiEnabled: boolean; aiLicensed: boolean }[] = [
  { name: "AI on", aiEnabled: true, aiLicensed: true },
  { name: "AI off", aiEnabled: false, aiLicensed: true },
  { name: "unlicensed", aiEnabled: true, aiLicensed: false },
  { name: "AI off and unlicensed", aiEnabled: false, aiLicensed: false },
];

const allGates = (on: boolean): Record<Gate, boolean> =>
  Object.fromEntries(GATES.map((gate) => [gate, on])) as Record<Gate, boolean>;

/** Every flag on, every flag off, then each flag alone. */
const FLAG_SETS: readonly { name: string; gates: Record<Gate, boolean> }[] = [
  { name: "all flags on", gates: allGates(true) },
  { name: "all flags off", gates: allGates(false) },
  ...GATES.map((gate) => ({ name: `${gate} alone`, gates: { ...allGates(false), [gate]: true } })),
];

const expectedLock = (reach: Reach, flags: NavFlags): string | null => {
  if (reach.lock === "license") return flags.aiLicensed ? null : "unlicensed";
  if (reach.lock !== "ai") return null;
  if (!flags.aiEnabled) return "ai_disabled";
  return flags.aiLicensed ? null : "unlicensed";
};

const show = (to: string, lock: string | null | undefined) => (lock ? `${to} [${lock}]` : to);

const expected = (role: Role, flags: NavFlags): string[] =>
  REACHABILITY
    .filter((reach) => reach.roles.includes(role) && (!reach.gate || flags[reach.gate]) && (!reach.xtmOneOnly || flags.xtmOne))
    .map((reach) => show(reach.to, expectedLock(reach, flags)))
    .sort();

const actual = (role: Role, flags: NavFlags): string[] =>
  navDestinations(resolveNav(flags, role === "admin", role === "groupManager"))
    .map((dest) => show(dest.to, dest.locked ? dest.lockReason : null))
    .sort();

test("every role, mode, flag set and AI state reaches exactly the pages of the table, with the same lock", () => {
  let combinations = 0;
  for (const role of ROLES) {
    for (const xtmOne of [false, true]) {
      for (const flagSet of FLAG_SETS) {
        for (const ai of AI_STATES) {
          // `intents` and `ctemCases` follow the mode, as Sidebar.tsx and use-nav-group.ts set them.
          const flags: NavFlags = { ...flagSet.gates, xtmOne, intents: xtmOne, ctemCases: xtmOne, aiEnabled: ai.aiEnabled, aiLicensed: ai.aiLicensed };
          const combination = `${role}, ${xtmOne ? "XTM One" : "Copilot"} mode, ${flagSet.name}, ${ai.name}`;
          assert.deepEqual(actual(role, flags), expected(role, flags), combination);
          combinations += 1;
        }
      }
    }
  }
  assert.equal(combinations, ROLES.length * 2 * FLAG_SETS.length * AI_STATES.length);
});

test("the palette lists each page once", () => {
  const flags: NavFlags = { ...allGates(true), xtmOne: true, intents: true, ctemCases: true, aiEnabled: true, aiLicensed: true };
  const routes = navDestinations(resolveNav(flags, true)).map((dest) => dest.to);
  assert.deepEqual(routes.filter((to, index) => routes.indexOf(to) !== index), []);
  const table = REACHABILITY.map((reach) => reach.to);
  assert.deepEqual(table.filter((to, index) => table.indexOf(to) !== index), [], "one line per page in the table");
});

test("the pages named by the program are pinned: Tasks, Fleets, Create an agent, the group managers' Analytics", () => {
  const find = (to: string) => REACHABILITY.find((reach) => reach.to === to);
  assert.deepEqual(find("/tasks"), { to: "/tasks", roles: EVERYONE, guard: "none" });
  assert.deepEqual(find("/fleets"), { to: "/fleets", roles: EVERYONE, lock: "license", guard: "none" });
  assert.deepEqual(find("/agents/new"), { to: "/agents/new", roles: EVERYONE, lock: "ai", guard: "none" });
  assert.deepEqual(find("/observability")?.roles, ADMIN_AND_GROUP_MANAGERS);
});

/** Each `<Route ...>` of `App.tsx` with its path and the text up to the next one. */
const appRoutes = (): { path: string; body: string }[] => {
  const source = readFileSync(new URL("../../App.tsx", import.meta.url), "utf8");
  return source
    .split("<Route ")
    .slice(1)
    .map((body) => ({ path: /^path="([^"]+)"/.exec(body)?.[1] ?? "", body }))
    .filter((route) => route.path !== "");
};

/** `/tasks/value/:tab?` serves `/tasks/value`: trailing optional segments do not change the page. */
const servesExactly = (path: string, to: string) => path === to || path.replace(/(\/:[A-Za-z]+\?)+$/, "") === to;

/** `/fleets/:fleetId/:tab?` serves `/fleets/x`: a parameter takes one segment. Tried after `servesExactly`. */
const serves = (path: string, to: string) => {
  const pattern = path
    .split("/")
    .slice(1)
    .map((segment) => (segment.startsWith(":") ? (segment.endsWith("?") ? "(?:/[^/]+)?" : "/[^/]+") : `/${segment}`))
    .join("");
  return new RegExp(`^${pattern}$`).test(to);
};

const guardOf = (body: string): Guard =>
  body.includes("<AdminRoute>") ? "AdminRoute" : body.includes("<AnalyticsRoute>") ? "AnalyticsRoute" : "none";

test("every destination is a route of App.tsx, behind the guard the table records", () => {
  const routes = appRoutes();
  for (const reach of REACHABILITY) {
    const route = routes.find((candidate) => servesExactly(candidate.path, reach.to))
      ?? routes.find((candidate) => serves(candidate.path, reach.to));
    assert.ok(route, `${reach.to} has a route in App.tsx`);
    assert.equal(guardOf(route.body), reach.guard, `${reach.to} route guard`);
  }
});

test("no route redirects or aliases a former address; an unknown one is not found (#797)", () => {
  const source = readFileSync(new URL("../../App.tsx", import.meta.url), "utf8");
  const routes = appRoutes();
  for (const retired of ["/flows/fleets/*", "/concierge/*", "/traces/sessions", "/profile/runners", "/public/*", "/analytics/:tab?", "/traces", "/trace-sessions", "/browser-profiles", "/variables", "/relays", "/users", "/users/:id/:tab?", "/groups", "/groups/:id/:tab?", "/runs/:id", "/assignments/:id/:tab?", "/flows/:id/builder", "/runners/:id"]) {
    assert.equal(routes.find((route) => route.path === retired), undefined, `${retired} is not a route`);
  }
  // The only redirects left are access guards (home, or the login page): never one page aliasing another.
  const targets = [...source.matchAll(/<Navigate to="([^"]*)"/g)].map((match) => match[1]);
  assert.deepEqual([...new Set(targets)].sort(), ["/", "/login"]);
  assert.doesNotMatch(source, /<Navigate to=\{/, "no computed redirect target");
  assert.doesNotMatch(source, /LegacyRedirect/);
  // The license splash has its own catch-all; the app's is the last one.
  const catchAlls = routes.filter((route) => route.path === "*");
  const catchAll = catchAlls[catchAlls.length - 1];
  assert.ok(catchAll, "a catch-all route exists");
  assert.match(catchAll.body, /<RouteNotFound \/>/);
  assert.ok(routes.some((route) => route.path === "/fleets"), "/fleets is a page of its own");
});

// One pattern, one component: the detail pages answer an unknown id with RouteNotFound. The
// rendered behaviour (a 404 never reaching the section boundary) is pages/unknown-id-not-found.test.tsx.
test("an unknown agent, assignment, run, flow or machine id is Page not found, never a page-local state (#797 S4)", () => {
  for (const page of ["AgentDetail.tsx", "AssignmentDetail.tsx", "RunDetail.tsx", "flow-builder/index.tsx", "RunnerDetail.tsx"]) {
    const source = readFileSync(new URL(`../../pages/${page}`, import.meta.url), "utf8");
    assert.match(source, /if \(!(agent|assignment|run|flow|runner)\) return <RouteNotFound \/>;/, `${page} renders RouteNotFound`);
    assert.doesNotMatch(source, /<NotFoundState/, `${page} draws no page-local not-found state`);
  }
});

test("Agentic Flow keeps the map and the library, never a Fleets tab (two rows never open one page)", () => {
  const flows = readFileSync(new URL("../../pages/Flows.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(flows, /key: "fleets"/);
  assert.doesNotMatch(flows, /FleetsTab/);
});

test("no role is listed a page its route guard refuses", () => {
  for (const reach of REACHABILITY) {
    if (reach.roles.includes("member")) assert.equal(reach.guard, "none", `${reach.to} is listed for every user`);
    if (reach.roles.includes("groupManager")) assert.notEqual(reach.guard, "AdminRoute", `${reach.to} is listed for group managers`);
  }
});
