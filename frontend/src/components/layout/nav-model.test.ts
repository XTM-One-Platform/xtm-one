/**
 * The left nav is the product's fixed structure - `yarn test:unit`.
 *
 * Every connected integration with a page of its own (an imported OpenAPI
 * spec, Nova's rules, OpenRouter's console) used to be listed after
 * "Integrations", named after the integration: each imported Swagger document
 * added a row to the menu. An integration's pages are tabs of its
 * detail page; the nav lists the Integrations page and nothing per integration.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";

import { NAV_SECTIONS, activeNavPath, navDestinations, navLabel, resolveNav, type NavFlags, type NavGroup } from "./nav-model.ts";

const FLAGS: NavFlags = {
  chat: true,
  customVariables: true,
  relays: true,
  xtmOne: false,
  aiAdoption: true,
  aiEnabled: true,
  aiLicensed: true,
  deepInvestigation: true,
  health: true,
  videoStudio: true,
  dashboardStudio: true,
  evaluation: true,
  intents: true,
  ctemCases: false,
};

const toolsGroup = (): NavGroup => {
  const group = resolveNav(FLAGS, true)
    .flatMap((section) => section.entries)
    .find((entry): entry is NavGroup => entry.kind === "group" && entry.id === "tools-and-integrations");
  assert.ok(group, "the Tools & Integrations group is listed");
  return group;
};

const group = (id: string, isAdmin = true, flags: NavFlags = FLAGS): NavGroup => {
  const found = resolveNav(flags, isAdmin)[0].entries
    .find((entry): entry is NavGroup => entry.kind === "group" && entry.id === id);
  assert.ok(found, `the ${id} group is in the top block`);
  return found;
};

test("the top block reads Home, Chat, the Orchestration group, then Work (#9301)", () => {
  const [primary] = resolveNav(FLAGS, true);
  assert.deepEqual(
    primary.entries.map((entry) => (entry.kind === "leaf" ? entry.to : entry.id)),
    ["/", "/chat", "agents", "work"],
  );
});

/* The callers set `ctemCases` from the mode (Sidebar.tsx, use-nav-group.ts), as `intents`. */
const XTM_ONE: NavFlags = { ...FLAGS, xtmOne: true, ctemCases: true };

test("CTEM: a top-level group between Chat and Orchestration in XTM One mode only, holding Cases, open with AI off (#1135)", () => {
  const xtmOne = XTM_ONE;
  const top = (flags: NavFlags, isAdmin = true) =>
    resolveNav(flags, isAdmin)[0].entries.map((entry) => (entry.kind === "leaf" ? entry.to : entry.id));
  assert.deepEqual(top(xtmOne), ["/", "/chat", "ctem", "agents", "work"], "XTM One mode, administrator");
  assert.deepEqual(top(xtmOne, false), ["/", "/chat", "ctem", "agents", "work"], "XTM One mode, every user");
  assert.deepEqual(top(FLAGS), ["/", "/chat", "agents", "work"], "Copilot mode: absent, not locked");
  const ctem = group("ctem", true, xtmOne);
  assert.equal(ctem.label, "CTEM");
  assert.equal(ctem.to, undefined, "no hub page: the heading folds the group");
  assert.deepEqual(ctem.children.map((child) => child.to), ["/ctem/cases", "/ctem/signals", "/ctem/inventory"]);
  for (const flags of [{ ...xtmOne, aiEnabled: false }, { ...xtmOne, aiLicensed: false }, { ...xtmOne, chat: false }]) {
    const found = group("ctem", true, flags);
    assert.ok(!found.locked, "the group carries no lock");
    const cases = found.children.find((child) => child.to === "/ctem/cases");
    assert.ok(cases && !cases.locked, "listed and open: a read surface, no AI or licence lock");
  }
  assert.equal(ctem.children[0].label, "Cases");
  assert.equal(ctem.children[1].label, "Signals", "between Cases and Inventory, Sam, 23 Nov 2026 (#1401)");
  assert.equal(ctem.children[2].label, "Inventory", "Sam, 22 Nov 2026 (#1178)");
  const glyphs = resolveNav(xtmOne, true)[0].entries.filter((entry) => entry.kind === "group" && entry.id !== "ctem").map((entry) => entry.icon);
  assert.ok(!glyphs.includes(ctem.icon), "the CTEM glyph is not another group's (Orchestration, Work)");
});

test("a case page lights Cases, the other CTEM pages still light Home (#1135)", () => {
  const sections = resolveNav(XTM_ONE, true);
  for (const pathname of ["/ctem/cases", "/ctem/cases/c-1", "/ctem/cases/c-1/history"]) {
    assert.equal(activeNavPath(sections, pathname), "/ctem/cases", pathname);
  }
  for (const pathname of ["/ctem", "/ctem/discovery", "/ctem/governance", "/ctem/casesx", "/briefing"]) {
    assert.equal(activeNavPath(sections, pathname), "/", pathname);
  }
});

test("the Signals page and its Sources page light CTEM > Signals (#1401)", () => {
  const sections = resolveNav(XTM_ONE, true);
  for (const pathname of ["/ctem/signals", "/ctem/signals/sources"]) {
    assert.equal(activeNavPath(sections, pathname), "/ctem/signals", pathname);
  }
  assert.equal(activeNavPath(sections, "/ctem/signalsx"), "/", "a longer path is not the page");
  const found = navDestinations(resolveNav(XTM_ONE, false)).find((dest) => dest.to === "/ctem/signals");
  assert.ok(found, "listed in the palette in XTM One mode");
  assert.equal(found.parent, "CTEM");
  assert.deepEqual(found.keywords, ["CTEM", "Trigger paths", "Signal sources"]);
  assert.equal(navDestinations(resolveNav(FLAGS, false)).find((dest) => dest.to === "/ctem/signals"), undefined, "never in Copilot mode");
});

test("the palette lists CTEM > Cases in XTM One mode only, found by CTEM and exposure cases (#1135)", () => {
  const destination = (flags: NavFlags) => navDestinations(resolveNav(flags, false)).find((dest) => dest.to === "/ctem/cases");
  const found = destination(XTM_ONE);
  assert.ok(found, "listed in XTM One mode");
  assert.equal(found.label, "Cases");
  assert.equal(found.parent, "CTEM", "the palette's secondary line names the group");
  assert.deepEqual(found.keywords, ["CTEM", "Exposure cases"]);
  assert.equal(destination(FLAGS), undefined, "never in Copilot mode");
});

test("the Orchestration group opens its hub, like Agent Studio, and holds Agents, Fleets, Agentic Flow and, for administrators, Intents (N2c, N2d)", () => {
  const agents = group("agents");
  assert.equal(agents.label, "Orchestration", "renamed in N2d; the id stays the persistence key");
  assert.equal(agents.to, "/agents-hub", "the hub path stays, so old links open");
  assert.equal(agents.locked, undefined, "the hub row carries no lock: each child carries its own");
  assert.deepEqual(agents.children.map((child) => child.to), ["/agents", "/fleets", "/flows", "/intents"]);
  assert.deepEqual(group("agents", false).children.map((child) => child.to), ["/agents", "/fleets", "/flows"]);
  for (const id of ["agents", "work"]) assert.ok(group(id).to, `${id} opens a hub`);
  // Create an agent is an action, not a row: a palette destination of the group.
  assert.deepEqual(agents.paletteOnly?.map((child) => child.to), ["/agents/new"]);
  const all = resolveNav(FLAGS, true).flatMap((section) => section.entries)
    .flatMap((entry) => (entry.kind === "group" ? entry.children.map((child) => child.to) : [entry.to]));
  assert.ok(!all.includes("/agents/new"), "no sidebar row creates an agent");
});

test("Intents: listed for administrators in XTM One mode only, never in Copilot mode, with no empty slot (#445)", () => {
  // The callers set `intents` from the mode (Sidebar.tsx, use-nav-group.ts): XTM One mode on, Copilot mode off.
  const xtmOne: NavFlags = { ...FLAGS, xtmOne: true, intents: true };
  const copilot: NavFlags = { ...FLAGS, xtmOne: false, intents: false };
  const rows = (flags: NavFlags, isAdmin: boolean) => group("agents", isAdmin, flags).children.map((child) => child.to);
  assert.deepEqual(rows(xtmOne, true), ["/agents", "/fleets", "/flows", "/intents"], "XTM One mode, administrator");
  assert.deepEqual(rows(xtmOne, false), ["/agents", "/fleets", "/flows"], "XTM One mode, not an administrator");
  assert.deepEqual(rows(copilot, true), ["/agents", "/fleets", "/flows"], "Copilot mode, administrator");
  assert.deepEqual(rows(copilot, false), ["/agents", "/fleets", "/flows"], "Copilot mode, not an administrator");
  // Agentic Flow is the last row of the group and Work follows it directly.
  assert.deepEqual(
    resolveNav(copilot, true)[0].entries.map((entry) => (entry.kind === "leaf" ? entry.to : entry.id)),
    ["/", "/chat", "agents", "work"],
  );
  const palette = (flags: NavFlags) => navDestinations(resolveNav(flags, true)).map((dest) => dest.to);
  assert.ok(palette(xtmOne).includes("/intents"), "the palette lists Intents in XTM One mode");
  assert.ok(!palette(copilot).includes("/intents"), "the palette never lists Intents in Copilot mode");
  // AI off keeps XTM One's row, marked: the mode decides the row, AI the lock.
  const aiOff = group("agents", true, { ...xtmOne, aiEnabled: false }).children.find((child) => child.to === "/intents");
  assert.equal(aiOff?.lockReason, "ai_disabled");
});

test("every row of a group carries its own lock: Fleets by the licence, Agentic Flow never, Intents by AI", () => {
  const lock = (flags: NavFlags, to: string) => {
    const child = group("agents", true, flags).children.find((entry) => entry.to === to);
    return child?.locked ? child.lockReason : null;
  };
  assert.equal(lock(FLAGS, "/fleets"), null);
  assert.equal(lock({ ...FLAGS, aiEnabled: false }, "/fleets"), null, "fleets stay readable with AI off");
  assert.equal(lock({ ...FLAGS, aiLicensed: false }, "/fleets"), "unlicensed");
  assert.equal(lock({ ...FLAGS, aiEnabled: false, aiLicensed: false }, "/flows"), null);
  assert.equal(lock({ ...FLAGS, aiEnabled: false }, "/intents"), "ai_disabled");
  assert.equal(lock({ ...FLAGS, aiEnabled: false }, "/agents"), "ai_disabled", "the list row keeps the lock the group row had");
  assert.equal(lock({ ...FLAGS, aiLicensed: false }, "/agents"), "unlicensed");
});

test("Tasks sits in Work right above Operations, both modes, every user, open with AI off; Value is a tab, no row", () => {
  // Sam, 20 Nov 2026 (#775): what to do and follow above what is running or past; the menu does not grow.
  const tasks = (flags: NavFlags, isAdmin: boolean) => group("work", isAdmin, flags).children.find((child) => child.to === "/tasks");
  for (const xtmOne of [false, true]) {
    const children = group("work", false, { ...FLAGS, xtmOne }).children.map((child) => child.to);
    assert.deepEqual(children.slice(0, 2), ["/tasks", "/operations"]);
    assert.ok(!children.includes("/value"), "Value is a Tasks tab, not a menu row");
    const entry = tasks({ ...FLAGS, xtmOne }, false);
    assert.ok(entry && !entry.locked, `listed and open (xtmOne ${xtmOne}, not admin)`);
  }
  const aiOff = tasks({ ...FLAGS, aiEnabled: false }, false);
  assert.ok(aiOff && !aiOff.locked, "AI off keeps the read surface open");
});

test("the command center pages light the home row; a fleet lights Fleets, a flow Agentic Flow", () => {
  const sections = resolveNav(FLAGS, true);
  for (const pathname of ["/", "/dashboard", "/briefing", "/ctem/discovery"]) {
    assert.equal(activeNavPath(sections, pathname), "/", pathname);
  }
  assert.equal(activeNavPath(sections, "/fleets/f-1/members"), "/fleets");
  assert.equal(activeNavPath(sections, "/flows/map/schedule"), "/flows");
  assert.equal(activeNavPath(sections, "/agents/new"), "/agents");
  assert.equal(activeNavPath(sections, "/tasks/value/methodology"), "/tasks");
  assert.equal(activeNavPath(sections, "/tasks/work-items/0b1c"), "/tasks");
  assert.equal(activeNavPath(sections, "/operations/shared-state"), "/operations");
  assert.equal(activeNavPath(sections, "/dashboards/d-1"), "/dashboards", "the studio is not the home");
});

test("the renames of #9301: Home, Administration, Audit logs; the studio keeps Dashboards", () => {
  const labels = navDestinations(resolveNav(FLAGS, true));
  const label = (to: string) => labels.find((dest) => dest.to === to)?.label;
  assert.equal(label("/"), "Home");
  assert.equal(label("/dashboards"), "Dashboards");
  assert.equal(label("/logs"), "Audit logs");
  assert.equal(labels.find((dest) => dest.to === "/logs")?.parent, "Administration");
  // The section id is the persistence key of its open state: unchanged.
  assert.ok(NAV_SECTIONS.some((section) => section.id === "admin" && section.label === "Administration"));
  for (const lang of ["en", "fr", "es", "ja"]) {
    const catalog = JSON.parse(readFileSync(new URL(`../../i18n/lang/${lang}.json`, import.meta.url), "utf8"));
    for (const key of ["Home", "Administration", "Audit logs"]) assert.ok(catalog[key], `${lang}: ${key}`);
  }
  const page = readFileSync(new URL("../../pages/AuditLogs.tsx", import.meta.url), "utf8");
  assert.match(page, /t_i18n\("Audit logs"\)/, "the h1 reads like its row");
});

test("only a collapsible section is named: the other blocks are separated by a rule alone", () => {
  // OpenCTI and OpenAEV draw no heading between blocks; a label on a plain
  // block would be the "RESOURCES" subtitle neither product has.
  for (const section of NAV_SECTIONS) {
    if (section.label) assert.ok(section.collapsible, `${section.id} is named but not collapsible`);
  }
});

test("Evaluations is listed, in the nav and the palette, only while evaluation is on", () => {
  const routes = (flags: NavFlags) => navDestinations(resolveNav(flags, true)).map((dest) => dest.to);
  assert.ok(routes(FLAGS).includes("/evaluations"));
  assert.ok(!routes({ ...FLAGS, evaluation: false }).includes("/evaluations"));
});

test("AI Adoption is listed in both modes, only while the feature is on", () => {
  // Generic AI (dev-docs/mode-split.md, S2): the mode never gates it.
  const routes = (flags: NavFlags) => navDestinations(resolveNav(flags, true)).map((dest) => dest.to);
  assert.ok(routes(FLAGS).includes("/ai-adoption"));
  assert.ok(routes({ ...FLAGS, xtmOne: true }).includes("/ai-adoption"));
  assert.ok(!routes({ ...FLAGS, aiAdoption: false }).includes("/ai-adoption"));
});

test("Dashboards is listed, in the nav and the palette, only while the Dashboard Studio is on", () => {
  const routes = (flags: NavFlags) => navDestinations(resolveNav(flags, false)).map((dest) => dest.to);
  assert.ok(routes(FLAGS).includes("/dashboards"));
  assert.ok(!routes({ ...FLAGS, dashboardStudio: false }).includes("/dashboards"));
});

const OBSERVABILITY = ["/observability", "/observability/outcomes", "/observability/traces", "/observability/sessions"];

const adminLeaf = (isAdmin: boolean, isGroupManager: boolean, to: string) =>
  resolveNav(FLAGS, isAdmin, isGroupManager)
    .find((section) => section.id === "admin")
    ?.entries.find((entry) => entry.kind === "leaf" && entry.to === to);

test("Observability is one family row: Model usage, Outcomes, Traces, Sessions (#9301 N3)", () => {
  const row = adminLeaf(true, false, "/observability");
  assert.ok(row && row.kind === "leaf");
  assert.equal(row.label, "Observability");
  assert.deepEqual(row.family?.map((member) => [member.to, member.label]), [
    ["/observability", "Model usage"],
    ["/observability/outcomes", "Outcomes"],
    ["/observability/traces", "Traces"],
    ["/observability/sessions", "Sessions"],
  ]);
  assert.equal(row.locked, undefined, "a read surface, never AI-locked (SC-011)");
  const palette = navDestinations(resolveNav(FLAGS, true)).filter((dest) => OBSERVABILITY.includes(dest.to));
  assert.deepEqual(palette.map((dest) => dest.to), OBSERVABILITY, "one palette destination per member page");
  assert.ok(palette.every((dest) => dest.parent === "Observability"), "each under the family's name");
});

test("every member page of Observability lights the family row", () => {
  const sections = resolveNav(FLAGS, true);
  for (const path of ["/observability", "/observability/outcomes", "/observability/traces", "/observability/traces/abc", "/observability/sessions"]) {
    assert.equal(activeNavPath(sections, path), "/observability", path);
  }
});

test("a group manager's Administration is the single row Model usage, the page's h1 (r3 A1)", () => {
  const row = adminLeaf(false, true, "/observability");
  assert.ok(row && row.kind === "leaf");
  assert.equal(row.label, "Model usage");
  assert.equal(row.family, undefined, "one member: no family, no tab row");
  const observability = (isAdmin: boolean, isGroupManager: boolean) =>
    navDestinations(resolveNav(FLAGS, isAdmin, isGroupManager))
      .map((dest) => dest.to)
      .filter((to) => OBSERVABILITY.includes(to));
  assert.deepEqual(observability(true, false), OBSERVABILITY);
  assert.deepEqual(observability(false, true), ["/observability"]);
  assert.deepEqual(observability(false, false), []);
  assert.deepEqual(
    resolveNav(FLAGS, false, true).find((section) => section.id === "admin")?.entries.length,
    1,
    "a group manager sees no other Administration entry",
  );
});

test("Tools & Integrations lists its fixed pages and nothing per integration", () => {
  const children = toolsGroup().children;
  assert.deepEqual(children.map((child) => child.to), ["/mcp-servers", "/tools", "/integrations", "/connections"]);
  assert.deepEqual(children[3].family?.map((member) => member.to), ["/connections", "/connections/variables", "/connections/relays"]);
});

test("Connections is one family row: Browser profiles, Variables, Relays (#9301 N3b)", () => {
  const row = toolsGroup().children.find((child) => child.to === "/connections");
  assert.ok(row);
  assert.equal(row.label, "Connections");
  assert.deepEqual(row.family?.map((member) => [member.to, member.label]), [
    ["/connections", "Browser profiles"],
    ["/connections/variables", "Variables"],
    ["/connections/relays", "Relays"],
  ]);
  assert.equal(row.locked, undefined, "infrastructure, never AI-locked");
  const sections = resolveNav(FLAGS, false);
  for (const path of ["/connections", "/connections/variables", "/connections/variables/v-1", "/connections/relays", "/connections/relays/r-1"]) {
    assert.equal(activeNavPath(sections, path), "/connections", path);
  }
  const palette = navDestinations(sections).filter((dest) => ["/connections", "/connections/variables", "/connections/relays"].includes(dest.to));
  assert.deepEqual(palette.map((dest) => [dest.to, dest.parent]), [
    ["/connections", "Connections"], ["/connections/variables", "Connections"], ["/connections/relays", "Connections"],
  ]);
  /* Both flags off: one member, a plain row with that page's name. */
  const alone = resolveNav({ ...FLAGS, customVariables: false, relays: false }, false)
    .flatMap((section) => section.entries)
    .find((entry): entry is NavGroup => entry.kind === "group" && entry.id === "tools-and-integrations")
    ?.children.find((child) => child.to === "/connections");
  assert.equal(alone?.label, "Browser profiles");
  assert.equal(alone?.family, undefined);
});

test("Users and groups is one family row for administrators only (#9301 N3b)", () => {
  const row = adminLeaf(true, false, "/users-and-groups");
  assert.ok(row && row.kind === "leaf");
  assert.equal(row.label, "Users and groups");
  assert.deepEqual(row.family?.map((member) => [member.to, member.label]), [["/users-and-groups", "Users"], ["/users-and-groups/groups", "Groups"]]);
  const sections = resolveNav(FLAGS, true);
  for (const path of ["/users-and-groups", "/users-and-groups/users/u-1", "/users-and-groups/groups", "/users-and-groups/groups/g-1", "/users-and-groups/groups/new"]) {
    assert.equal(activeNavPath(sections, path), "/users-and-groups", path);
  }
  assert.equal(adminLeaf(false, true, "/users-and-groups"), undefined, "a group manager gets no Users and groups row");
});

test("no nav entry or palette destination opens one integration's page", () => {
  const routes = [
    ...NAV_SECTIONS.flatMap((section) => section.entries).flatMap((entry) => (
      entry.kind === "group" ? [entry.to, ...entry.children.map((child) => child.to)] : [entry.to]
    )),
    ...navDestinations(resolveNav(FLAGS, true)).map((dest) => dest.to),
  ];
  assert.deepEqual(routes.filter((to) => to?.startsWith("/integrations/")), []);
});

test("an integration's pages, its own tabs included, highlight the Integrations entry", () => {
  const sections = resolveNav(FLAGS, true);
  for (const pathname of ["/integrations", "/integrations/i-1", "/integrations/i-1/api-spec", "/integrations/i-1/rules"]) {
    assert.equal(activeNavPath(sections, pathname), "/integrations", pathname);
  }
});

test("the nav is built without reading the integrations", () => {
  for (const file of ["./nav-model.ts", "./Sidebar.tsx", "./GlobalSearch.tsx"]) {
    const source = readFileSync(new URL(file, import.meta.url), "utf8");
    assert.doesNotMatch(source, /useIntegrations\b|PROVIDER_EXTRA_TABS|useIntegrationApi/, file);
  }
});

/* Every row the menu can show, group rows included, whatever the flags or the role. */
const menuRows = () => NAV_SECTIONS.flatMap((section) => [
  ...(section.icon ? [{ label: section.label ?? section.id, icon: section.icon }] : []),
  ...section.entries.flatMap((entry) => (entry.kind === "group" ? [entry, ...entry.children] : [entry])),
]);

test("no group repeats one of its rows: neither its label nor its icon (#9301 N2d)", () => {
  for (const entry of NAV_SECTIONS.flatMap((section) => section.entries)) {
    if (entry.kind !== "group") continue;
    for (const child of entry.children) {
      assert.notEqual(child.label, entry.label, `${entry.label} has a row of the same name`);
      assert.notEqual(child.icon, entry.icon, `${child.label} carries the icon of its group ${entry.label}`);
    }
  }
});

test("no two rows of the menu share an icon (#9301 N2d)", () => {
  const seen = new Map<unknown, string>();
  for (const row of menuRows()) {
    const other = seen.get(row.icon);
    assert.equal(other, undefined, `${row.label} and ${other} share an icon`);
    seen.set(row.icon, row.label);
  }
});

test("the palette finds the Orchestration group by its former name (#9301 N2d)", () => {
  const hub = navDestinations(resolveNav(FLAGS, true)).find((dest) => dest.to === "/agents-hub");
  assert.ok(hub);
  assert.equal(hub.label, "Orchestration");
  assert.deepEqual(hub.keywords, ["Agents"]);
});

/* Every page that renders a nav family header, found by scanning the pages. */
const familyMemberPages = () => {
  const root = new URL("../../pages/", import.meta.url);
  return readdirSync(root, { encoding: "utf8", recursive: true })
    .filter((file) => file.endsWith(".tsx") && !file.endsWith(".test.tsx"))
    .map((file) => ({ file, source: readFileSync(new URL(file, root), "utf8") }))
    .filter(({ source }) => /<NavFamilyHeader\b/.test(source));
};

test("every family member page puts its header at one offset, so switching tabs never moves it (#10409 UX r1 B1)", () => {
  const pages = familyMemberPages();
  assert.ok(pages.length >= 3, "the Observability members render the family header");
  for (const { file, source } of pages) {
    const roots = [...source.matchAll(/return \(\s*<div className="([^"]*)">\s*(?:\{\/\*[\s\S]*?\*\/\}\s*)?<NavFamilyHeader\b/g)];
    assert.ok(roots.length > 0, `${file}: the family header is the first child of the page root`);
    for (const [, className] of roots) assert.equal(className, "space-y-6", `${file}: page root of a family member`);
  }
});

test("the family header spaces its title and tab row as Settings does (#10409 UX r1 B2)", () => {
  const source = readFileSync(new URL("./NavFamilyHeader.tsx", import.meta.url), "utf8");
  assert.match(source, /return \(\s*<div className="space-y-6">/);
});

test("the Platform Health entry is a translated label at /health, never a name, and /concierge is no destination (#101 S1)", () => {
  const translate = (id: string) => `t(${id})`;
  const destinations = navDestinations(resolveNav(FLAGS, true));
  const health = destinations.find((d) => d.to === "/health");
  assert.ok(health);
  assert.equal(health.text, undefined);
  assert.equal(navLabel(health, translate), "t(Platform Health)");
  assert.equal(destinations.find((d) => d.to === "/concierge"), undefined);
});
